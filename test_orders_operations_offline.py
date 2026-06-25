"""
Offline unit tests for shopify.operations.orders.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the orders operations are callable from non-MCP entry
points (Story 10.29 / A5, AC4). The two reads (GET_ORDERS and GET_ORDER_BY_ID)
share the order-node core selection, so it is factored into one OrderCoreFields
fragment both spread (AC3 — the fragment-reuse decision is pinned by
test_shared_order_core_fragment_is_reused). orders is a read-only domain — no
mutations.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_orders_operations_offline.py -v
"""

from _testing import FakeClient
from shopify.operations import orders as ops
from shopify.queries import orders as q

# ---------- AC3: a shared order-node core fragment IS extracted and reused ----


def test_shared_order_core_fragment_is_reused():
    """The id/name/createdAt/total selection is identical across the two reads,
    so it lives in one OrderCoreFields fragment that both GET queries spread."""
    assert "fragment OrderCoreFields on Order" in q.ORDER_CORE_FIELDS
    for query in (q.GET_ORDERS, q.GET_ORDER_BY_ID):
        assert "...OrderCoreFields" in query
        # the fragment definition is embedded in the query document it's used in
        assert q.ORDER_CORE_FIELDS.strip() in query


# ---------- read operations (build vars + execute, return structured data) ----


def test_read_orders_builds_first_var_and_returns_nodes():
    resp = {
        "orders": {
            "nodes": [
                {"id": "gid://shopify/Order/1001", "name": "#1001"},
                {"id": "gid://shopify/Order/1002", "name": "#1002"},
            ]
        }
    }
    fc = FakeClient([resp])
    out = ops.read_orders(fc, 20)
    assert out == resp["orders"]["nodes"]
    assert fc.calls[0][0] == q.GET_ORDERS
    assert fc.calls[0][1] == {"first": 20}


def test_read_orders_passes_through_the_callers_first_unchanged():
    """read_orders does no clamping — it executes with the count the tool hands
    it (the tool owns the ≤250 clamp), so a value of 250 flows straight through."""
    fc = FakeClient([{"orders": {"nodes": []}}])
    ops.read_orders(fc, 250)
    assert fc.calls[0][1] == {"first": 250}


def test_read_orders_empty_returns_empty_list():
    fc = FakeClient([{"orders": {"nodes": []}}])
    assert ops.read_orders(fc, 20) == []


def test_read_orders_missing_connection_returns_empty_list():
    """Defensive: a permissions-trimmed / shape-drifted response with no orders
    connection yields an empty list rather than raising."""
    fc = FakeClient([{}])
    assert ops.read_orders(fc, 20) == []


def test_read_order_paginates_with_order_gid_and_page_cap():
    resp = {
        "order": {
            "id": "gid://shopify/Order/1001",
            "name": "#1001",
            "lineItems": {
                "nodes": [{"name": "Tee", "quantity": 1}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
    }
    fc = FakeClient([resp])
    order, line_items, capped = ops.read_order(fc, "1001")
    assert order == resp["order"]
    assert line_items == [{"name": "Tee", "quantity": 1}]
    assert capped is False
    assert fc.calls[0][0] == q.GET_ORDER_BY_ID
    assert fc.calls[0][1] == {"id": "gid://shopify/Order/1001", "first": 50, "after": None}
    assert ops.ORDER_LINE_ITEMS_PAGE_SIZE == 50


def test_read_order_missing_order_returns_none_and_empty_items():
    """A deleted / wrong id yields ``{"order": null}`` — surfaced as (None, [], False)."""
    fc = FakeClient([{"order": None}])
    order, line_items, capped = ops.read_order(fc, "999")
    assert order is None
    assert line_items == []
    assert capped is False


# ---- A3 / Story 10.34: per-order line-item cap detection on the list read ----
# GET_ORDERS caps each order's lineItems at a fixed first:50 and cannot paginate
# that nested-in-list connection. Rather than silently truncate, the query now
# selects pageInfo.hasNextPage and the operations layer surfaces which orders were
# capped, so tools/orders.py can warn — parity with read_order's `capped` flag.


def test_get_orders_query_selects_lineitems_pageinfo():
    """GET_ORDERS must select lineItems.pageInfo.hasNextPage so the list path can
    DETECT the fixed first:50 cap (it cannot paginate the nested connection)."""
    assert "pageInfo" in q.GET_ORDERS
    assert "hasNextPage" in q.GET_ORDERS


def _node_with_line_item_page(oid, has_next):
    return {
        "id": f"gid://shopify/Order/{oid}",
        "lineItems": {
            "nodes": [{"name": "Tee", "quantity": 1}],
            "pageInfo": {"hasNextPage": has_next},
        },
    }


def test_capped_line_item_order_ids_flags_orders_past_the_cap():
    """An order whose lineItems.pageInfo.hasNextPage is True is reported capped,
    named by its gid (the tool layer applies from_gid for display)."""
    orders = [_node_with_line_item_page("1001", has_next=True)]
    assert ops.capped_line_item_order_ids(orders) == ["gid://shopify/Order/1001"]


def test_capped_line_item_order_ids_empty_when_within_cap():
    orders = [_node_with_line_item_page("1001", has_next=False)]
    assert ops.capped_line_item_order_ids(orders) == []


def test_capped_line_item_order_ids_returns_only_truncated_ids():
    """Mixed batch: only the over-cap order's id comes back; an order with
    hasNextPage False and one missing pageInfo entirely are both treated as
    not-capped (defensive against shape drift / permissions-trimmed responses)."""
    orders = [
        _node_with_line_item_page("1001", has_next=True),
        _node_with_line_item_page("1002", has_next=False),
        {"id": "gid://shopify/Order/1003", "lineItems": {"nodes": []}},  # no pageInfo
    ]
    assert ops.capped_line_item_order_ids(orders) == ["gid://shopify/Order/1001"]


def test_capped_line_item_order_ids_empty_for_empty_batch():
    assert ops.capped_line_item_order_ids([]) == []
