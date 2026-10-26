"""Order, InventoryItem and Location ids accept a number or a GID of their own type (Story 10.110).

A bare ``to_gid(<Type>, x)`` wrapped a full GID again, for example
``gid://shopify/Order/gid://shopify/Order/7``, and passed a GID of another type
through. Shopify then refused the id or found nothing, and the reply did not say
why. Each id now goes through ``shopify._ids`` (the same rule as webhook ids in
Story 10.109): a numeric id or a full GID of the right type is sent with one
prefix, and anything else is refused before any network call.
"""

import pytest

from shopify_mcp.shopify.operations import inventory as inv_ops
from shopify_mcp.shopify.operations import orders as order_ops
from shopify_mcp.tools import inventory, orders
from tests.support import CapturingServer, FakeClient

_LOC = "gid://shopify/Location/5"
_ITEM = "gid://shopify/InventoryItem/7"
_LEVEL = {
    "location": {"id": _LOC, "name": "Main"},
    "quantities": [{"name": "available", "quantity": 3}],
}
_INV_ITEM = {"inventoryItem": {"id": _ITEM, "inventoryLevels": {"nodes": [_LEVEL]}}}
_SET_OK = {"inventorySetOnHandQuantities": {"userErrors": [], "inventoryAdjustmentGroup": {}}}
_PRODUCT = {
    "product": {
        "id": "gid://shopify/Product/1",
        "title": "T",
        "variants": {
            "nodes": [{"id": "gid://shopify/ProductVariant/9", "title": "V", **_INV_ITEM}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }
}


def _good(resource_type: str, n: str) -> list[str]:
    gid = f"gid://shopify/{resource_type}/{n}"
    return [n, f" {n} ", gid, f" {gid} "]


def _bad(resource_type: str, n: str) -> list[str]:
    return [
        f"gid://shopify/ProductVariant/{n}",
        f" gid://shopify/ProductVariant/{n}",
        f"foo/{n}",
        f"gid://shopify/{resource_type}/",
        f"gid://shopify/{resource_type}/{n}/1",
        "",
        "   ",
        "abc",
        chr(0xFF17),  # full-width "7": isdigit() is True, so isascii() refuses it
    ]


def _tools(module, responses):
    srv = CapturingServer()
    fc = FakeClient(responses)
    module.register(srv, fc)
    return srv.tools, fc


# ---------- get_order: order_id ----------


@pytest.mark.parametrize("order_id", _good("Order", "7"))
def test_get_order_sends_one_order_gid(order_id):
    tools, fc = _tools(orders, [{"order": None}])
    tools["get_order"](order_id=order_id)
    assert [c[1]["id"] for c in fc.calls] == ["gid://shopify/Order/7"]


@pytest.mark.parametrize("order_id", _bad("Order", "7"))
def test_get_order_refuses_a_wrong_id_before_any_call(order_id):
    tools, fc = _tools(orders, [{"order": None}])
    out = tools["get_order"](order_id=order_id)
    assert fc.calls == []
    assert out.startswith("Error:")
    assert "Order GID" in out
    assert "7" not in out


# ---------- update_inventory: inventory_item_id and location_id ----------


@pytest.mark.parametrize("inventory_item_id", _good("InventoryItem", "7"))
def test_update_inventory_sends_one_inventory_item_gid(inventory_item_id):
    tools, fc = _tools(inventory, [_INV_ITEM, _SET_OK])
    out = tools["update_inventory"](
        inventory_item_id=inventory_item_id, location_id="5", quantity=1, confirm=True
    )
    assert out.startswith("CONFIRMED —")
    assert fc.calls[0][1] == {"id": _ITEM}
    assert fc.calls[1][1]["input"]["setQuantities"][0]["inventoryItemId"] == _ITEM


@pytest.mark.parametrize("location_id", _good("Location", "5"))
def test_update_inventory_sends_one_location_gid_and_finds_the_level(location_id):
    tools, fc = _tools(inventory, [_INV_ITEM, _SET_OK])
    preview = tools["update_inventory"](inventory_item_id="7", location_id=location_id, quantity=1)
    assert "Current quantity  : 3\n" in preview
    tools, fc = _tools(inventory, [_INV_ITEM, _SET_OK])
    tools["update_inventory"](
        inventory_item_id="7", location_id=location_id, quantity=1, confirm=True
    )
    assert fc.calls[1][1]["input"]["setQuantities"][0]["locationId"] == _LOC


@pytest.mark.parametrize("inventory_item_id", _bad("InventoryItem", "7"))
@pytest.mark.parametrize("confirm", [False, True])
def test_update_inventory_refuses_a_wrong_inventory_item_id(inventory_item_id, confirm):
    tools, fc = _tools(inventory, [_INV_ITEM, _SET_OK])
    out = tools["update_inventory"](
        inventory_item_id=inventory_item_id, location_id="5", quantity=1, confirm=confirm
    )
    assert fc.calls == []
    assert out.startswith("Error:")
    assert "InventoryItem GID" in out
    assert "7" not in out


@pytest.mark.parametrize("location_id", _bad("Location", "5"))
@pytest.mark.parametrize("confirm", [False, True])
def test_update_inventory_refuses_a_wrong_location_id(location_id, confirm):
    tools, fc = _tools(inventory, [_INV_ITEM, _SET_OK])
    out = tools["update_inventory"](
        inventory_item_id="7", location_id=location_id, quantity=1, confirm=confirm
    )
    assert fc.calls == []
    assert out.startswith("Error:")
    assert "Location GID" in out
    assert "5" not in out


# ---------- update_variant_inventory_quantity: location_id ----------


@pytest.mark.parametrize("location_id", _good("Location", "5"))
def test_variant_quantity_location_filter_matches_the_level(location_id):
    tools, fc = _tools(inventory, [_PRODUCT, _SET_OK])
    out = tools["update_variant_inventory_quantity"](
        product_id="1", quantity=0, location_id=location_id, confirm=True
    )
    assert "Unresolved location" not in out
    assert fc.calls[1][1]["input"]["setQuantities"][0]["locationId"] == _LOC


# location_id is optional here, and "" already means "omitted" (every location),
# so it is not a bad id for this tool.
@pytest.mark.parametrize("location_id", [v for v in _bad("Location", "5") if v])
@pytest.mark.parametrize("confirm", [False, True])
def test_variant_quantity_refuses_a_wrong_location_id_before_any_call(location_id, confirm):
    tools, fc = _tools(inventory, [_PRODUCT, _SET_OK])
    out = tools["update_variant_inventory_quantity"](
        product_id="1", quantity=0, location_id=location_id, confirm=confirm
    )
    assert fc.calls == []
    assert out.startswith("Error:")
    assert "Location GID" in out
    assert "5" not in out


def test_variant_quantity_without_location_id_still_covers_every_level():
    tools, fc = _tools(inventory, [_PRODUCT])
    out = tools["update_variant_inventory_quantity"](product_id="1", quantity=0)
    assert "@ Main" in out
    assert len(fc.calls) == 1


# ---------- operations layer ----------


@pytest.mark.parametrize("order_id", _good("Order", "7"))
def test_read_order_sends_one_order_gid(order_id):
    fc = FakeClient([{"order": None}])
    order_ops.read_order(fc, order_id)
    assert fc.calls[0][1]["id"] == "gid://shopify/Order/7"


@pytest.mark.parametrize("inventory_item_id", _good("InventoryItem", "7"))
def test_read_inventory_item_levels_sends_one_inventory_item_gid(inventory_item_id):
    fc = FakeClient([_INV_ITEM])
    inv_ops.read_inventory_item_levels(fc, inventory_item_id)
    assert fc.calls[0][1] == {"id": _ITEM}


@pytest.mark.parametrize(
    ("read", "resource_type"),
    [(order_ops.read_order, "Order"), (inv_ops.read_inventory_item_levels, "InventoryItem")],
)
def test_operations_refuse_a_wrong_id_before_any_call(read, resource_type):
    for value in _bad(resource_type, "7"):
        fc = FakeClient([])
        with pytest.raises(ValueError):
            read(fc, value)
        assert fc.calls == [], value
