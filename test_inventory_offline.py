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

import re

import pytest

from tools import inventory
from tools.inventory import (
    GET_PRODUCT_INVENTORY,
    GET_INVENTORY_ITEM,
    SET_INVENTORY,
    UPDATE_INVENTORY_ITEM_TRACKED,
    _available_qty,
)
from _testing import CapturingServer, FakeClient


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    """Keep tests from polluting aon_mcp_log.txt."""
    monkeypatch.setattr(inventory, "log_write", lambda *a, **k: None)


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


def _variant(vid, title, sku, levels, tracked=True):
    return {
        "id": f"gid://shopify/ProductVariant/{vid}",
        "title": title,
        "sku": sku,
        "inventoryItem": {
            "id": f"gid://shopify/InventoryItem/{vid}",
            "tracked": tracked,
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


# ---- update_variant_inventory_tracking ----

def _tracked_update_ok(inv_item_gid, tracked):
    return {"inventoryItemUpdate": {
        "inventoryItem": {"id": inv_item_gid, "tracked": tracked},
        "userErrors": [],
    }}


def _tracked_update_err(field, message):
    return {"inventoryItemUpdate": {
        "inventoryItem": None,
        "userErrors": [{"field": field, "message": message}],
    }}


def test_tracking_product_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["update_variant_inventory_tracking"](
        product_id="999", tracked=True, confirm=True,
    )
    assert out == "No product found with id 999."
    assert len(fc.calls) == 1, "no mutation should run on missing product"


def test_tracking_preview_lists_all_variants_when_ids_omitted():
    """variant_ids=None → operate on every variant of the product."""
    variants = [
        _variant("100", "S", "REEF-S", [], tracked=False),
        _variant("101", "M", "REEF-M", [], tracked=False),
        _variant("102", "L", "REEF-L", [], tracked=False),
    ]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=False,
    )
    assert "PREVIEW" in out
    assert "Target  : tracked=True" in out
    assert "Would change (3):" in out
    assert "Unchanged (0):" in out
    assert "False → True" in out
    # Preview must not issue mutations
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_INVENTORY


def test_tracking_confirm_issues_one_mutation_per_changed_variant():
    variants = [
        _variant("100", "S", "REEF-S", [], tracked=False),
        _variant("101", "M", "REEF-M", [], tracked=False),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _tracked_update_ok("gid://shopify/InventoryItem/100", True),
        _tracked_update_ok("gid://shopify/InventoryItem/101", True),
    ])
    out = tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Changed (2):" in out
    # First call is the read; mutations come after with the right GIDs.
    mutation_calls = fc.calls[1:]
    assert len(mutation_calls) == 2
    for i, vid in enumerate(("100", "101")):
        query, vars_ = mutation_calls[i]
        assert query == UPDATE_INVENTORY_ITEM_TRACKED
        assert vars_ == {
            "id": f"gid://shopify/InventoryItem/{vid}",
            "input": {"tracked": True},
        }


def test_tracking_unchanged_variants_get_no_mutation():
    """Variants already at the target state are reported as unchanged; no
    mutation is issued for them. Saves API calls on repeat runs."""
    variants = [
        _variant("100", "S", "REEF-S", [], tracked=True),   # already at target
        _variant("101", "M", "REEF-M", [], tracked=False),  # needs change
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _tracked_update_ok("gid://shopify/InventoryItem/101", True),
    ])
    out = tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=True,
    )
    assert "Changed (1):" in out
    assert "Unchanged (1):" in out
    # Only one mutation: for variant 101
    assert len(fc.calls) == 2
    assert fc.calls[1][1]["id"] == "gid://shopify/InventoryItem/101"


def test_tracking_all_already_at_target_issues_no_mutations():
    variants = [
        _variant("100", "S", "REEF-S", [], tracked=True),
        _variant("101", "M", "REEF-M", [], tracked=True),
    ]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Changed (0):" in out
    assert "Unchanged (2):" in out
    # Only the initial read ran
    assert len(fc.calls) == 1


def test_tracking_variant_ids_filter_applies():
    """Caller-supplied variant_ids filter the targets; unknown ids are
    surfaced in an 'Unresolved' block and skipped."""
    variants = [
        _variant("100", "S", "REEF-S", [], tracked=False),
        _variant("101", "M", "REEF-M", [], tracked=False),
        _variant("102", "L", "REEF-L", [], tracked=False),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _tracked_update_ok("gid://shopify/InventoryItem/100", True),
    ])
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        variant_ids=["100", "999"],  # 999 is not on this product
        confirm=True,
    )
    assert "Changed (1):" in out
    assert "Unresolved variant ids:" in out
    assert "999" in out
    # Only variant 100 should have been mutated
    assert len(fc.calls) == 2
    assert fc.calls[1][1]["id"] == "gid://shopify/InventoryItem/100"


def test_tracking_partial_failure_reports_per_variant():
    """One failed mutation must not abort the others. Aggregate report lists
    each outcome."""
    variants = [
        _variant("100", "S", "REEF-S", [], tracked=False),
        _variant("101", "M", "REEF-M", [], tracked=False),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _tracked_update_ok("gid://shopify/InventoryItem/100", True),
        _tracked_update_err("inventoryItemId", "locked by another process"),
    ])
    out = tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Changed (1):" in out
    assert "Failed (1):" in out
    assert "locked by another process" in out


def test_tracking_preview_only_issues_exactly_one_execute_call():
    """confirm=False must not issue any mutations, even if there are targets."""
    variants = [_variant("100", "S", "REEF-S", [], tracked=False)]
    tools, fc = _build([_product_with_variants(variants)])
    tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=False,
    )
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_INVENTORY


def test_tracking_product_with_no_variants_issues_no_mutations():
    """Product exists but has zero variants — confirm is a clean no-op."""
    tools, fc = _build([_product_with_variants([])])
    out = tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Changed (0):" in out
    assert "Unchanged (0):" in out
    assert len(fc.calls) == 1, "only the read should run when there are no targets"


def test_tracking_transport_error_mid_loop_does_not_abort_batch():
    """If client.execute raises on variant N (network glitch, 5xx), prior
    successes must still be reported and subsequent variants must still be
    attempted — otherwise a flaky store silently leaves partial state with no
    log line or user-facing confirmation."""
    variants = [
        _variant("100", "S", "REEF-S", [], tracked=False),
        _variant("101", "M", "REEF-M", [], tracked=False),
        _variant("102", "L", "REEF-L", [], tracked=False),
    ]

    class FlakyFakeClient:
        def __init__(self):
            self.calls = []
        def execute(self, query, variables=None):
            self.calls.append((query, variables))
            if len(self.calls) == 1:
                return _product_with_variants(variants)
            if len(self.calls) == 3:
                # Variant 101's mutation throws
                raise RuntimeError("upstream 502")
            # Variants 100 and 102 succeed
            return _tracked_update_ok(variables["id"], True)

    srv = CapturingServer()
    fc = FlakyFakeClient()
    inventory.register(srv, fc)
    out = srv.tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=True,
    )
    assert out.startswith("CONFIRMED"), out
    assert "Changed (2):" in out
    assert "Failed (1):" in out
    assert "transport error" in out and "upstream 502" in out
    # All three mutations attempted (1 read + 3 mutation attempts)
    assert len(fc.calls) == 4


def test_tracking_unresolved_variant_ids_are_deduped():
    """A caller that supplies the same unknown id twice should see it reported
    once, not twice. Mirrors the dedup behavior in update_variant_inventory_policy."""
    variants = [_variant("100", "S", "REEF-S", [], tracked=False)]
    tools, fc = _build([
        _product_with_variants(variants),
        _tracked_update_ok("gid://shopify/InventoryItem/100", True),
    ])
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        variant_ids=["100", "999", "999", "999"],
        confirm=True,
    )
    # 999 should appear exactly once in the unresolved block — match the full
    # bulleted line so a substring like "• 9999" from a future test wouldn't
    # incorrectly satisfy the old `count("• 999") == 1` assertion.
    assert len(re.findall(r"^    • 999$", out, re.MULTILINE)) == 1


def test_tracking_duplicate_known_variant_ids_are_deduped():
    """A caller that supplies the same known id twice must not mutate it twice."""
    variants = [_variant("100", "S", "REEF-S", [], tracked=False)]
    tools, fc = _build([
        _product_with_variants(variants),
        _tracked_update_ok("gid://shopify/InventoryItem/100", True),
    ])
    tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        variant_ids=["100", "100", "100"],
        confirm=True,
    )
    # Exactly one mutation (plus the read)
    assert len(fc.calls) == 2
    assert fc.calls[1][1]["id"] == "gid://shopify/InventoryItem/100"


def test_tracking_variant_ids_order_is_preserved():
    """Caller-supplied variant_ids order must drive mutation order (the old
    set-comprehension pattern produced non-deterministic ordering)."""
    variants = [
        _variant("100", "S", "REEF-S", [], tracked=False),
        _variant("101", "M", "REEF-M", [], tracked=False),
        _variant("102", "L", "REEF-L", [], tracked=False),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _tracked_update_ok("gid://shopify/InventoryItem/102", True),
        _tracked_update_ok("gid://shopify/InventoryItem/100", True),
        _tracked_update_ok("gid://shopify/InventoryItem/101", True),
    ])
    tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        variant_ids=["102", "100", "101"],
        confirm=True,
    )
    mutation_ids = [fc.calls[i][1]["id"] for i in range(1, 4)]
    assert mutation_ids == [
        "gid://shopify/InventoryItem/102",
        "gid://shopify/InventoryItem/100",
        "gid://shopify/InventoryItem/101",
    ]


def test_tracking_variant_missing_inventory_item_id_is_reported_failed():
    """A variant whose inventoryItem has no id can't be mutated — report it
    in the Failed block and keep going. Shape is defensive: Shopify normally
    returns a valid id, but a stale cache or partial response mustn't crash
    the batch."""
    bad_variant = {
        "id": "gid://shopify/ProductVariant/100",
        "title": "S",
        "sku": "REEF-S",
        # inventoryItem present but no id key — Shopify normally returns one,
        # but a partial response (or future schema shift) mustn't crash the batch.
        "inventoryItem": {"tracked": False, "inventoryLevels": {"nodes": []}},
    }
    good_variant = _variant("101", "M", "REEF-M", [], tracked=False)
    tools, fc = _build([
        _product_with_variants([bad_variant, good_variant]),
        _tracked_update_ok("gid://shopify/InventoryItem/101", True),
    ])
    out = tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Changed (1):" in out
    assert "Failed (1):" in out
    assert "variant has no inventoryItem id" in out
    # Only the good variant triggers a mutation (plus the initial read).
    assert len(fc.calls) == 2
    assert fc.calls[1][1]["id"] == "gid://shopify/InventoryItem/101"


def test_tracking_preview_renders_when_inventory_item_id_is_null():
    """Regression: Shopify can return `inventoryItem: {"id": null, ...}` on a
    partial or permissions-trimmed response. The preview's `_variant_line`
    helper calls `from_gid(inv_item.get("id", ""))`, and .get only applies the
    default for *missing* keys — a present-but-null value flows through. The
    previous from_gid crashed on None.split(...); the preview must now render
    cleanly with an empty inventory_item segment."""
    variant_with_null_inv_id = {
        "id": "gid://shopify/ProductVariant/100",
        "title": "NullId",
        "sku": "NULL",
        "inventoryItem": {
            "id": None,
            "tracked": False,
            "inventoryLevels": {"nodes": []},
        },
    }
    tools, fc = _build([_product_with_variants([variant_with_null_inv_id])])
    out = tools["update_variant_inventory_tracking"](
        product_id="555", tracked=True, confirm=False,
    )
    assert "PREVIEW" in out
    assert "NullId" in out
    assert "variant_id: 100" in out
    # The null inventory_item id renders as empty (two spaces between the
    # label and the ` — ` separator), proving from_gid(None) returned ""
    # rather than crashing.
    assert "inventory_item:  — False → True" in out
    assert len(fc.calls) == 1


# ---- update_variant_inventory_quantity ----

def _set_inventory_ok():
    return {"inventorySetOnHandQuantities": {
        "inventoryAdjustmentGroup": {"createdAt": "2026-04-22T00:00:00Z"},
        "userErrors": [],
    }}


def _set_inventory_err(field, message):
    return {"inventorySetOnHandQuantities": {
        "inventoryAdjustmentGroup": None,
        "userErrors": [{"field": field, "message": message}],
    }}


def test_quantity_product_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["update_variant_inventory_quantity"](
        product_id="999", quantity=0, confirm=True,
    )
    assert out == "No product found with id 999."
    assert len(fc.calls) == 1


def test_quantity_preview_lists_all_variant_location_pairs_when_filters_omitted():
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
        _variant("101", "M", "REEF-M", [_level(3, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, confirm=False,
    )
    assert "PREVIEW" in out
    assert "Target qty  : 0" in out
    assert "Target loc  : all locations" in out
    assert "Would change (2):" in out
    assert "5 → 0" in out and "3 → 0" in out
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_INVENTORY


def test_quantity_confirm_issues_single_batch_set_inventory_call():
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
        _variant("101", "M", "REEF-M", [_level(3, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _set_inventory_ok(),
    ])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Changed (2):" in out
    # Exactly one mutation call (one read + one SET).
    assert len(fc.calls) == 2
    query, vars_ = fc.calls[1]
    assert query == SET_INVENTORY
    assert vars_["input"]["reason"] == "correction"
    set_qtys = vars_["input"]["setQuantities"]
    assert len(set_qtys) == 2
    assert {q["inventoryItemId"] for q in set_qtys} == {
        "gid://shopify/InventoryItem/100",
        "gid://shopify/InventoryItem/101",
    }
    assert all(q["quantity"] == 0 for q in set_qtys)
    assert all(q["locationId"] == "gid://shopify/Location/9" for q in set_qtys)


def test_quantity_unchanged_pairs_skip_mutation():
    """Variants already at target qty are reported as unchanged and excluded
    from the setQuantities batch."""
    variants = [
        _variant("100", "S", "REEF-S", [_level(0, "gid://shopify/Location/9")]),  # already 0
        _variant("101", "M", "REEF-M", [_level(3, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _set_inventory_ok(),
    ])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, confirm=True,
    )
    assert "Changed (1):" in out
    assert "Unchanged (1):" in out
    set_qtys = fc.calls[1][1]["input"]["setQuantities"]
    assert len(set_qtys) == 1
    assert set_qtys[0]["inventoryItemId"] == "gid://shopify/InventoryItem/101"


def test_quantity_all_already_at_target_issues_no_mutation():
    """confirm=True with nothing to change reports CONFIRMED no-op without
    issuing an empty setQuantities batch (Shopify would reject that)."""
    variants = [
        _variant("100", "S", "REEF-S", [_level(0, "gid://shopify/Location/9")]),
        _variant("101", "M", "REEF-M", [_level(0, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, confirm=True,
    )
    assert "no-op" in out
    assert "Changed     : (none" in out
    assert len(fc.calls) == 1


def test_quantity_location_filter_narrows_to_one_location():
    """With location_id set, only levels at that location are considered."""
    variants = [
        _variant("100", "S", "REEF-S", [
            _level(5, "gid://shopify/Location/9"),
            _level(2, "gid://shopify/Location/77"),
        ]),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _set_inventory_ok(),
    ])
    tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, location_id="9", confirm=True,
    )
    set_qtys = fc.calls[1][1]["input"]["setQuantities"]
    assert len(set_qtys) == 1
    assert set_qtys[0]["locationId"] == "gid://shopify/Location/9"


def test_quantity_unresolved_location_block_when_filter_matches_nothing():
    """If the caller filters to a location that exists on no variant, the
    preview surfaces an Unresolved location note so they don't silently no-op."""
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, location_id="9999", confirm=False,
    )
    assert "Unresolved location: 9999" in out
    assert "Would change (0):" in out


def test_quantity_variant_ids_filter_applies_and_unresolved_ids_surface():
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
        _variant("101", "M", "REEF-M", [_level(3, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _set_inventory_ok(),
    ])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, variant_ids=["100", "999"], confirm=True,
    )
    assert "Changed (1):" in out
    assert "Unresolved variant ids:" in out
    assert "999" in out
    set_qtys = fc.calls[1][1]["input"]["setQuantities"]
    assert len(set_qtys) == 1
    assert set_qtys[0]["inventoryItemId"] == "gid://shopify/InventoryItem/100"


def test_quantity_surfaces_user_errors_as_error_string():
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _set_inventory_err(
            ["input", "setQuantities", "0", "quantity"],
            "Quantity must be non-negative",
        ),
    ])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=-1, confirm=True,
    )
    assert out.startswith("Error:")
    assert "Quantity must be non-negative" in out


def test_quantity_variant_with_no_levels_contributes_no_pair():
    """A variant with inventoryLevels=[] has no (variant, location) pair, so
    it's not in `to_change` nor `unchanged` — just silently absent."""
    variants = [
        _variant("100", "S", "REEF-S", []),  # no levels at all
        _variant("101", "M", "REEF-M", [_level(3, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _set_inventory_ok(),
    ])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, confirm=True,
    )
    # Only variant 101 contributes a pair.
    assert "Changed (1):" in out
    set_qtys = fc.calls[1][1]["input"]["setQuantities"]
    assert len(set_qtys) == 1
    assert set_qtys[0]["inventoryItemId"] == "gid://shopify/InventoryItem/101"


def test_quantity_multi_location_default_touches_every_variant_location_pair():
    """location_id omitted + variant with 2 levels → both pairs in the batch."""
    variants = [
        _variant("100", "S", "REEF-S", [
            _level(5, "gid://shopify/Location/9"),
            _level(2, "gid://shopify/Location/77"),
        ]),
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _set_inventory_ok(),
    ])
    tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, confirm=True,
    )
    set_qtys = fc.calls[1][1]["input"]["setQuantities"]
    assert len(set_qtys) == 2
    assert {q["locationId"] for q in set_qtys} == {
        "gid://shopify/Location/9",
        "gid://shopify/Location/77",
    }


def test_quantity_preview_only_issues_the_read():
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build([_product_with_variants(variants)])
    tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, confirm=False,
    )
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_INVENTORY


def test_quantity_variant_with_missing_inventory_item_id_is_skipped_not_batched():
    """A pair with a null inventoryItem.id (inventory tracking disabled) would
    poison the whole setQuantities batch. It must be reported as Skipped and
    excluded from the mutation, while the healthy pairs still go through."""
    variants = [
        # Healthy variant — should be written
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
        # Variant with inventory tracking disabled (inventoryItem=None)
        {
            "id": "gid://shopify/ProductVariant/101",
            "title": "Broken",
            "sku": "BRK",
            "inventoryItem": None,
        },
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _set_inventory_ok(),
    ])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, confirm=True,
    )
    # Accounting must be accurate: Changed reflects only written pairs.
    assert "Changed (1):" in out
    # The null-inv-item variant doesn't have a location level pair in the read
    # output (because its inventoryItem is None → no inventoryLevels to
    # iterate), so it doesn't appear as Skipped either — it just never makes it
    # into `pairs`. Healthy variant still gets written.
    set_qtys = fc.calls[1][1]["input"]["setQuantities"]
    assert len(set_qtys) == 1
    assert set_qtys[0]["inventoryItemId"] == "gid://shopify/InventoryItem/100"


def test_quantity_skipped_pair_when_location_id_is_null_on_level():
    """If a level has location=None (shouldn't happen in practice), the pair
    is moved to Skipped and excluded from the batch — neither silently dropped
    nor allowed to poison the batch with a null locationId."""
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
        # Variant whose level has no location (pathological shape)
        {
            "id": "gid://shopify/ProductVariant/101",
            "title": "Broken",
            "sku": "BRK",
            "inventoryItem": {
                "id": "gid://shopify/InventoryItem/101",
                "tracked": True,
                "inventoryLevels": {"nodes": [{
                    "quantities": [{"name": "available", "quantity": 3}],
                    "location": None,
                }]},
            },
        },
    ]
    tools, fc = _build([
        _product_with_variants(variants),
        _set_inventory_ok(),
    ])
    out = tools["update_variant_inventory_quantity"](
        product_id="555", quantity=0, confirm=True,
    )
    assert "Changed (1):" in out
    assert "Skipped (1):" in out
    assert "missing inventoryItem.id or location.id" in out
    # Only the healthy variant is in the batch
    set_qtys = fc.calls[1][1]["input"]["setQuantities"]
    assert len(set_qtys) == 1
    assert set_qtys[0]["inventoryItemId"] == "gid://shopify/InventoryItem/100"
