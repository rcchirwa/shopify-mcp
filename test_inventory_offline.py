"""
Offline unit tests for tools/inventory.py.

Uses a scripted FakeClient to exercise the 2024-07+ `quantities(names: [...])`
shape, preview/confirm gate, and userErrors surfacing — no Shopify API calls,
no .env required.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_inventory_offline.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from tools import inventory
from tools.inventory import (
    GET_PRODUCT_INVENTORY,
    GET_INVENTORY_ITEM,
    SET_INVENTORY,
    _available_qty,
)


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    """Keep tests from polluting aon_mcp_log.txt."""
    monkeypatch.setattr(inventory, "log_write", lambda *a, **k: None)


class CapturingServer:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, query, variables=None):
        self.calls.append((query, variables))
        if not self.responses:
            raise AssertionError("FakeClient: unexpected extra execute() call")
        return self.responses.pop(0)


def _build(responses):
    srv = CapturingServer()
    fc = FakeClient(responses)
    inventory.register(srv, fc)
    return srv.tools, fc


# ---- Fixture builders ----

def _level(available, location_gid="gid://shopify/Location/9"):
    """Build an InventoryLevel node in the 2024-07+ `quantities` shape."""
    return {
        "quantities": [{"name": "available", "quantity": available}],
        "location": {"id": location_gid, "name": "Main"},
    }


def _product_with_variants(variants):
    """Build the GetProductInventory response for a product with the given variants."""
    return {
        "product": {
            "title": "Luminous Reef Tee",
            "variants": {"nodes": variants},
        }
    }


def _variant(vid, title, sku, levels):
    return {
        "id": f"gid://shopify/ProductVariant/{vid}",
        "title": title,
        "sku": sku,
        "inventoryItem": {
            "id": f"gid://shopify/InventoryItem/{vid}",
            "inventoryLevels": {"nodes": levels},
        },
    }


# ---- _available_qty helper ----

def test_available_qty_returns_quantity_for_available_name():
    assert _available_qty({"quantities": [{"name": "available", "quantity": 7}]}) == 7


def test_available_qty_ignores_other_names():
    level = {"quantities": [
        {"name": "on_hand", "quantity": 10},
        {"name": "available", "quantity": 3},
    ]}
    assert _available_qty(level) == 3


def test_available_qty_returns_none_when_no_available_entry():
    assert _available_qty({"quantities": [{"name": "on_hand", "quantity": 10}]}) is None


def test_available_qty_returns_none_on_empty_or_missing_quantities():
    assert _available_qty({}) is None
    assert _available_qty({"quantities": None}) is None
    assert _available_qty({"quantities": []}) is None


# ---- get_inventory ----

def test_get_inventory_renders_variant_lines_with_available_quantity():
    tools, fc = _build([_product_with_variants([
        _variant("100", "Small", "REEF-S", [_level(12)]),
        _variant("101", "Medium", "REEF-M", [_level(0)]),
    ])])
    out = tools["get_inventory"](product_id="555")
    assert "Luminous Reef Tee" in out
    assert "Small" in out and "available: 12" in out
    assert "Medium" in out and "available: 0" in out
    assert "variant_id: 100" in out
    assert "variant_id: 101" in out
    # Query is the new 2024-07+ shape and was called with a full GID
    assert fc.calls[0][0] == GET_PRODUCT_INVENTORY
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/555"}


def test_get_inventory_falls_back_to_na_when_no_levels():
    tools, fc = _build([_product_with_variants([
        _variant("200", "OneSize", "ONE", []),
    ])])
    out = tools["get_inventory"](product_id="555")
    assert "available: N/A" in out


def test_get_inventory_falls_back_to_na_when_available_name_missing():
    """Defensive: if Shopify returns a level without an 'available' entry, render N/A, not crash."""
    tools, fc = _build([_product_with_variants([
        _variant("300", "Weird", "W", [{
            "quantities": [{"name": "on_hand", "quantity": 5}],
            "location": {"id": "gid://shopify/Location/9", "name": "Main"},
        }]),
    ])])
    out = tools["get_inventory"](product_id="555")
    assert "available: N/A" in out


def test_get_inventory_handles_null_nested_fields_without_crashing():
    """Defensive: if Shopify returns null for variants / inventoryItem /
    inventoryLevels on a valid product (e.g. inventory tracking disabled),
    render gracefully instead of crashing on a None.get() chain."""
    tools, fc = _build([{"product": {
        "title": "Tracking Off",
        "variants": {"nodes": [
            # Variant with inventory tracking disabled → inventoryItem is null
            {
                "id": "gid://shopify/ProductVariant/500",
                "title": "NoTrack",
                "sku": "NOTRACK",
                "inventoryItem": None,
            },
            # Variant where inventoryLevels itself is null
            {
                "id": "gid://shopify/ProductVariant/501",
                "title": "NullLevels",
                "sku": "NULL",
                "inventoryItem": {
                    "id": "gid://shopify/InventoryItem/501",
                    "inventoryLevels": None,
                },
            },
        ]},
    }}])
    out = tools["get_inventory"](product_id="555")
    assert "Tracking Off" in out
    assert "NoTrack" in out and "available: N/A" in out
    assert "NullLevels" in out


def test_get_inventory_returns_not_found_on_missing_product():
    tools, fc = _build([{"product": None}])
    out = tools["get_inventory"](product_id="999")
    assert out == "Product 999 not found."


# ---- update_inventory ----

def _inventory_item_response(available, location_id="9"):
    return {
        "inventoryItem": {
            "inventoryLevels": {"nodes": [{
                "quantities": [{"name": "available", "quantity": available}],
                "location": {"id": f"gid://shopify/Location/{location_id}"},
            }]}
        }
    }


def test_update_inventory_preview_shows_current_qty_from_quantities_and_does_not_mutate():
    tools, fc = _build([_inventory_item_response(available=5)])
    out = tools["update_inventory"](
        inventory_item_id="42", location_id="9", quantity=0, confirm=False,
    )
    assert "PREVIEW" in out
    assert "Current quantity  : 5" in out
    assert "New quantity      : 0" in out
    assert "To apply, call again with confirm=True." in out
    # Only the GET ran; no SET
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_INVENTORY_ITEM


def test_update_inventory_preview_unknown_when_no_matching_location():
    # Inventory item response has location 9 but we ask about location 77
    tools, fc = _build([_inventory_item_response(available=5, location_id="9")])
    out = tools["update_inventory"](
        inventory_item_id="42", location_id="77", quantity=3, confirm=False,
    )
    assert "Current quantity  : unknown" in out


def test_update_inventory_confirmed_calls_set_inventory_with_correct_gids():
    tools, fc = _build([
        _inventory_item_response(available=5),
        {"inventorySetOnHandQuantities": {
            "inventoryAdjustmentGroup": {"createdAt": "2026-04-22T00:00:00Z"},
            "userErrors": [],
        }},
    ])
    out = tools["update_inventory"](
        inventory_item_id="42", location_id="9", quantity=0, confirm=True,
    )
    assert out.startswith("Done.")
    # Second call was the mutation
    mutation_query, mutation_vars = fc.calls[1]
    assert mutation_query == SET_INVENTORY
    assert mutation_vars == {
        "input": {
            "reason": "correction",
            "setQuantities": [{
                "inventoryItemId": "gid://shopify/InventoryItem/42",
                "locationId": "gid://shopify/Location/9",
                "quantity": 0,
            }],
        }
    }


def test_update_inventory_handles_missing_inventory_item_without_crashing():
    """If the inventory_item_id doesn't exist (deleted / wrong id), Shopify
    returns {"inventoryItem": None}. The preview should render cleanly with
    'unknown' rather than crash on a None.get() chain."""
    tools, fc = _build([{"inventoryItem": None}])
    out = tools["update_inventory"](
        inventory_item_id="999999", location_id="9", quantity=5, confirm=False,
    )
    assert "PREVIEW" in out
    assert "Current quantity  : unknown" in out
    assert "confirm=True" in out


def test_update_inventory_surfaces_user_errors_as_error_string():
    tools, fc = _build([
        _inventory_item_response(available=5),
        {"inventorySetOnHandQuantities": {
            "inventoryAdjustmentGroup": None,
            "userErrors": [
                {"field": ["input", "setQuantities", "0", "quantity"],
                 "message": "Quantity must be non-negative"},
            ],
        }},
    ])
    out = tools["update_inventory"](
        inventory_item_id="42", location_id="9", quantity=-1, confirm=True,
    )
    assert out.startswith("Error:")
    assert "Quantity must be non-negative" in out
