"""
Offline unit tests for shopify.operations.inventory.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the inventory operations are callable from non-MCP
entry points (Story 10.28 / A5, AC4). The two reads (GET_PRODUCT_INVENTORY and
GET_INVENTORY_ITEM) share the 2024-07+ "available" inventory-level selection, so
it is factored into one InventoryLevelQuantities fragment both spread (AC3 — the
fragment-reuse decision is pinned by test_shared_inventory_level_fragment).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_inventory_operations_offline.py -v
"""

from _testing import FakeClient
from shopify.operations import inventory as ops
from shopify.queries import inventory as q

# ---------- AC3: a shared inventory-level fragment IS extracted and reused ----


def test_shared_inventory_level_fragment_is_reused():
    """The available-qty selection on InventoryLevel is identical across the two
    reads, so it lives in one fragment that both GET queries spread."""
    assert "fragment InventoryLevelQuantities on InventoryLevel" in q.INVENTORY_LEVEL_QUANTITIES
    for query in (q.GET_PRODUCT_INVENTORY, q.GET_INVENTORY_ITEM):
        assert "...InventoryLevelQuantities" in query
        # the fragment definition is embedded in the query document it's used in
        assert q.INVENTORY_LEVEL_QUANTITIES.strip() in query


def test_mutations_do_not_carry_the_read_fragment():
    """The fragment is read-only; the mutations neither declare nor spread it."""
    for mutation in (q.UPDATE_INVENTORY_ITEM_TRACKED, q.SET_INVENTORY):
        assert "InventoryLevelQuantities" not in mutation


# ---------- read operations (build vars + execute, return structured data) ----


def test_read_product_inventory_paginates_with_product_gid_and_page_cap():
    resp = {
        "product": {
            "title": "Luminous Reef Tee",
            "variants": {
                "nodes": [{"id": "gid://shopify/ProductVariant/100", "title": "S"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
    }
    fc = FakeClient([resp])
    product, variants, capped = ops.read_product_inventory(fc, "555")
    assert product == resp["product"]
    assert variants == [{"id": "gid://shopify/ProductVariant/100", "title": "S"}]
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCT_INVENTORY
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/555", "first": 50, "after": None}
    assert ops.VARIANTS_PAGE_CAP == 50


def test_read_product_inventory_missing_product_returns_none():
    fc = FakeClient([{"product": None}])
    product, variants, capped = ops.read_product_inventory(fc, "555")
    assert product is None
    assert variants == []


def test_read_inventory_item_levels_builds_gid_and_returns_inventory_item():
    inv_item = {"inventoryLevels": {"nodes": [{"location": {"id": "gid://shopify/Location/9"}}]}}
    fc = FakeClient([{"inventoryItem": inv_item}])
    out = ops.read_inventory_item_levels(fc, "42")
    assert out == inv_item
    assert fc.calls[0][0] == q.GET_INVENTORY_ITEM
    assert fc.calls[0][1] == {"id": "gid://shopify/InventoryItem/42"}


def test_read_inventory_item_levels_returns_none_when_item_absent():
    """A deleted / wrong id yields ``{"inventoryItem": null}`` — surfaced as None."""
    fc = FakeClient([{"inventoryItem": None}])
    assert ops.read_inventory_item_levels(fc, "42") is None


# ---------- write operations (build input + execute, return raw result) -------


def test_update_inventory_item_tracked_builds_input_and_returns_raw_result():
    raw = {"inventoryItemUpdate": {"inventoryItem": {"id": "g", "tracked": True}, "userErrors": []}}
    fc = FakeClient([raw])
    out = ops.update_inventory_item_tracked(fc, "gid://shopify/InventoryItem/7", True)
    assert out is raw
    assert fc.calls[0][0] == q.UPDATE_INVENTORY_ITEM_TRACKED
    assert fc.calls[0][1] == {
        "id": "gid://shopify/InventoryItem/7",
        "input": {"tracked": True},
    }


def test_set_inventory_on_hand_wraps_set_quantities_and_returns_raw_result():
    raw = {"inventorySetOnHandQuantities": {"inventoryAdjustmentGroup": {}, "userErrors": []}}
    fc = FakeClient([raw])
    set_quantities = [
        {
            "inventoryItemId": "gid://shopify/InventoryItem/42",
            "locationId": "gid://shopify/Location/9",
            "quantity": 0,
        }
    ]
    out = ops.set_inventory_on_hand(fc, set_quantities)
    assert out is raw
    assert fc.calls[0][0] == q.SET_INVENTORY
    assert fc.calls[0][1] == {"input": {"reason": "correction", "setQuantities": set_quantities}}
