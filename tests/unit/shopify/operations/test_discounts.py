"""
Offline unit tests for shopify.operations.discounts.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the discounts operations are callable from non-MCP
entry points (Story 10.27 / A5, AC4). discounts has no by-id/by-handle read pair
and no duplicated selection set, so no shared GraphQL fragment is extracted
(AC3 — the "no forced fragment" decision is pinned by test_no_shared_fragment).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/shopify/operations/test_discounts.py -v
"""

from shopify_mcp.shopify.operations import discounts as ops
from shopify_mcp.shopify.queries import discounts as q
from tests.support import FakeClient

# ---------- AC3: no shared fragment applies to discounts ----------


def test_no_shared_fragment_in_discount_queries():
    """discounts has no by-id/by-handle pair and no duplicated selection set, so
    none of its GraphQL strings declares or spreads a fragment. GET_CODE_DISCOUNTS
    uses inline fragments (`... on DiscountCodeBasic`) to select type-specific
    fields off the `Discount` union — those are exempted, unlike a spread
    (`...FragmentName`) of a *named* fragment declaration."""
    for query in (q.CREATE_PRICE_RULE, q.CREATE_DISCOUNT_CODE):
        assert "fragment " not in query
        assert "..." not in query
    assert "fragment " not in q.GET_CODE_DISCOUNTS


# ---------- read operations (build vars + execute, return node list) ----------


def _discount_node(
    gid, title, status="ACTIVE", codes=None, percentage=None, ends_at=None, usage_limit=None
):
    discount = {
        "__typename": "DiscountCodeBasic",
        "title": title,
        "status": status,
        "endsAt": ends_at,
        "usageLimit": usage_limit,
        "codes": {"nodes": [{"code": c} for c in (codes or [title])]},
    }
    if percentage is not None:
        discount["customerGets"] = {
            "value": {"__typename": "DiscountPercentage", "percentage": percentage}
        }
    return {"id": f"gid://shopify/DiscountCodeNode/{gid}", "discount": discount}


def test_read_code_discounts_returns_nodes_and_uses_fixed_page_size():
    fc = FakeClient([{"discountNodes": {"nodes": [_discount_node("1", "Spring", percentage=0.2)]}}])
    out = ops.read_code_discounts(fc)
    assert out == [_discount_node("1", "Spring", percentage=0.2)]
    assert fc.calls[0][0] == q.GET_CODE_DISCOUNTS
    assert fc.calls[0][1] == {
        "first": ops.CODE_DISCOUNTS_PAGE_SIZE,
        "query": "method:code",
        "codesFirst": ops.DISCOUNT_CODES_PER_DISCOUNT_CAP,
    }
    assert ops.CODE_DISCOUNTS_PAGE_SIZE == 50
    assert ops.DISCOUNT_CODES_PER_DISCOUNT_CAP == 10


def test_read_code_discounts_empty_returns_empty_list():
    fc = FakeClient([{"discountNodes": {"nodes": []}}])
    assert ops.read_code_discounts(fc) == []


def test_read_code_discounts_missing_connection_returns_empty_list():
    """Defensive: a permissions-trimmed / shape-drifted response with no
    discountNodes connection yields an empty list rather than raising."""
    fc = FakeClient([{}])
    assert ops.read_code_discounts(fc) == []


# ---------- write operations (build input + execute, return raw result) -------


def test_create_price_rule_wraps_input_and_returns_raw_result():
    raw = {"priceRuleCreate": {"priceRule": {"id": "gid://shopify/PriceRule/1"}}}
    fc = FakeClient([raw])
    price_rule_input = {"title": "T", "valueType": "PERCENTAGE", "value": "-20"}
    out = ops.create_price_rule(fc, price_rule_input)
    assert out is raw
    assert fc.calls[0][0] == q.CREATE_PRICE_RULE
    assert fc.calls[0][1] == {"input": price_rule_input}


def test_create_price_rule_discount_code_builds_vars_and_returns_raw_result():
    raw = {"priceRuleDiscountCodeCreate": {"priceRuleDiscountCode": {"code": "LAUNCH20"}}}
    fc = FakeClient([raw])
    out = ops.create_price_rule_discount_code(fc, "gid://shopify/PriceRule/1", "LAUNCH20")
    assert out is raw
    assert fc.calls[0][0] == q.CREATE_DISCOUNT_CODE
    assert fc.calls[0][1] == {
        "priceRuleId": "gid://shopify/PriceRule/1",
        "code": "LAUNCH20",
    }
