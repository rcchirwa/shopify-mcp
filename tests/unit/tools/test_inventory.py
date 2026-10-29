"""
Offline unit tests for tools/inventory.py.

Uses a scripted FakeClient to exercise the 2024-07+ `quantities(names: [...])`
shape, preview/confirm gate, and userErrors surfacing — no Shopify API calls,
no .env required.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_inventory.py -v
"""

import re

import pytest

from shopify_mcp.tools import inventory
from shopify_mcp.tools._scrub import REFLECT_MAX_LEN
from shopify_mcp.tools._untrusted import INJECTION_REMINDER
from shopify_mcp.tools.inventory import (
    GET_INVENTORY_ITEM,
    GET_PRODUCT_INVENTORY,
    SET_INVENTORY,
    UPDATE_INVENTORY_ITEM_TRACKED,
    _level_qty,
)
from tests.support import CapturingServer, FakeClient, fenced


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


def _quantities(on_hand, committed=0):
    """The `quantities` list the shared fragment reads. Shopify derives
    available = on_hand - committed."""
    return [
        {"name": "available", "quantity": on_hand - committed},
        {"name": "on_hand", "quantity": on_hand},
        {"name": "committed", "quantity": committed},
    ]


def _level(on_hand, location_gid="gid://shopify/Location/9", committed=0, name="Main"):
    """Build an InventoryLevel node in the 2024-07+ `quantities` shape."""
    return {
        "quantities": _quantities(on_hand, committed),
        "location": {"id": location_gid, "name": name},
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


# ---- _level_qty helper ----


def test_level_qty_returns_the_quantity_for_the_named_entry():
    level = {"quantities": _quantities(on_hand=10, committed=7)}
    assert _level_qty(level, "on_hand") == 10
    assert _level_qty(level, "committed") == 7
    assert _level_qty(level, "available") == 3


def test_level_qty_returns_none_when_the_name_is_missing():
    assert _level_qty({"quantities": [{"name": "available", "quantity": 10}]}, "on_hand") is None


def test_level_qty_returns_none_on_empty_or_missing_quantities():
    assert _level_qty({}, "on_hand") is None
    assert _level_qty({"quantities": None}, "on_hand") is None
    assert _level_qty({"quantities": []}, "on_hand") is None


# ---- get_inventory ----


def test_get_inventory_renders_on_hand_committed_and_available_per_location():
    """Story 10.111: one sub-line per location, with on-hand and committed as
    well as available, so a level with committed units is visible."""
    tools, fc = _build(
        [
            _product_with_variants(
                [
                    _variant(
                        "100",
                        "Small",
                        "REEF-S",
                        [
                            _level(12),
                            _level(2, "gid://shopify/Location/77", committed=2, name="Annex"),
                        ],
                    ),
                    _variant("101", "Medium", "REEF-M", [_level(0)]),
                ]
            )
        ]
    )
    out = tools["get_inventory"](product_id="555")
    assert out == (
        "Inventory for: Luminous Reef Tee (id: 555)\n"
        "\n"
        "  • Small — SKU: REEF-S — variant_id: 100\n"
        "      @ Main (location: 9) — on-hand: 12 — committed: 0 — available: 12\n"
        "      @ Annex (location: 77) — on-hand: 2 — committed: 2 — available: 0\n"
        "  • Medium — SKU: REEF-M — variant_id: 101\n"
        "      @ Main (location: 9) — on-hand: 0 — committed: 0 — available: 0"
    ), out
    # Query is the new 2024-07+ shape and was called with a full GID
    assert fc.calls[0][0] == GET_PRODUCT_INVENTORY
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/555", "first": 50, "after": None}


def test_get_inventory_says_so_when_a_variant_has_no_levels():
    tools, fc = _build(
        [
            _product_with_variants(
                [
                    _variant("200", "OneSize", "ONE", []),
                ]
            )
        ]
    )
    out = tools["get_inventory"](product_id="555")
    assert out.endswith("  • OneSize — SKU: ONE — variant_id: 200\n      (no inventory levels)"), (
        out
    )


def test_get_inventory_names_a_location_by_id_when_its_name_is_missing():
    level = {"quantities": _quantities(4), "location": {"id": "gid://shopify/Location/9"}}
    tools, fc = _build([_product_with_variants([_variant("200", "S", "S1", [level])])])
    out = tools["get_inventory"](product_id="555")
    assert "      @ id 9 (location: 9) — on-hand: 4 — committed: 0 — available: 4" in out, out


def test_get_inventory_falls_back_to_na_when_a_quantity_name_is_missing():
    """Defensive: if Shopify returns a level without some quantity names, render N/A, not crash."""
    tools, fc = _build(
        [
            _product_with_variants(
                [
                    _variant(
                        "300",
                        "Weird",
                        "W",
                        [
                            {
                                "quantities": [{"name": "on_hand", "quantity": 5}],
                                "location": {"id": "gid://shopify/Location/9", "name": "Main"},
                            }
                        ],
                    ),
                ]
            )
        ]
    )
    out = tools["get_inventory"](product_id="555")
    assert "— on-hand: 5 — committed: N/A — available: N/A" in out, out


def test_get_inventory_handles_null_nested_fields_without_crashing():
    """Defensive: if Shopify returns null for variants / inventoryItem /
    inventoryLevels on a valid product (e.g. inventory tracking disabled),
    render gracefully instead of crashing on a None.get() chain."""
    tools, fc = _build(
        [
            {
                "product": {
                    "title": "Tracking Off",
                    "variants": {
                        "nodes": [
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
                        ]
                    },
                }
            }
        ]
    )
    out = tools["get_inventory"](product_id="555")
    assert "Tracking Off" in out
    assert "  • NoTrack — SKU: NOTRACK — variant_id: 500\n      (no inventory levels)" in out
    assert "  • NullLevels — SKU: NULL — variant_id: 501\n      (no inventory levels)" in out


def test_get_inventory_returns_not_found_on_missing_product():
    tools, fc = _build([{"product": None}])
    out = tools["get_inventory"](product_id="999")
    assert out == "Product 999 not found."


def test_get_inventory_warns_when_variant_pagination_is_capped():
    """After max_pages (10) pages with hasNextPage=True, paginate() returns
    capped=True and get_inventory appends a WARNING line."""

    def _paged_product(cursor):
        return {
            "product": {
                "title": "Huge Product",
                "variants": {
                    "nodes": [_variant("100", "S", "S-SKU", [_level(1)])],
                    "pageInfo": {"hasNextPage": True, "endCursor": cursor},
                },
            }
        }

    tools, fc = _build([_paged_product(f"c{i}") for i in range(10)])
    out = tools["get_inventory"](product_id="555")
    assert "WARNING" in out and "additional variants" in out
    assert len(fc.calls) == 10


# ---- update_inventory ----


def _inventory_item_response(on_hand, location_id="9", committed=0):
    return {
        "inventoryItem": {
            "inventoryLevels": {
                "nodes": [
                    {
                        "quantities": _quantities(on_hand, committed),
                        "location": {"id": f"gid://shopify/Location/{location_id}"},
                    }
                ]
            }
        }
    }


def test_update_inventory_preview_shows_current_qty_from_quantities_and_does_not_mutate():
    tools, fc = _build([_inventory_item_response(on_hand=5)])
    out = tools["update_inventory"](
        inventory_item_id="42",
        location_id="9",
        quantity=0,
        confirm=False,
    )
    assert "PREVIEW" in out
    assert "Current on-hand   : 5 (committed: 0, available: 5)\n" in out
    assert "New on-hand       : 0" in out
    assert "To apply, call again with confirm=True." in out
    # Only the GET ran; no SET
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_INVENTORY_ITEM


def test_update_inventory_preview_unknown_when_no_matching_location():
    # Inventory item response has location 9 but we ask about location 77
    tools, fc = _build([_inventory_item_response(on_hand=5, location_id="9")])
    out = tools["update_inventory"](
        inventory_item_id="42",
        location_id="77",
        quantity=3,
        confirm=False,
    )
    assert "Current on-hand   : unknown" in out


def test_update_inventory_preview_shows_committed_units_beside_on_hand():
    """Story 10.111: the write sets on-hand, so the preview's current value is
    on-hand too, with committed and available beside it."""
    tools, fc = _build([_inventory_item_response(on_hand=2, committed=2)])
    out = tools["update_inventory"](inventory_item_id="42", location_id="9", quantity=0)
    assert "  Current on-hand   : 2 (committed: 2, available: 0)\n" in out, out
    assert "  New on-hand       : 0" in out, out


def test_update_inventory_preview_marks_a_missing_on_hand_unknown():
    level = {
        "quantities": [{"name": "available", "quantity": 3}],
        "location": {"id": "gid://shopify/Location/9"},
    }
    tools, fc = _build([{"inventoryItem": {"inventoryLevels": {"nodes": [level]}}}])
    out = tools["update_inventory"](inventory_item_id="42", location_id="9", quantity=0)
    assert "  Current on-hand   : unknown (committed: N/A, available: 3)\n" in out, out


def test_update_inventory_confirmed_calls_set_inventory_with_correct_gids():
    tools, fc = _build(
        [
            _inventory_item_response(on_hand=5),
            {
                "inventorySetOnHandQuantities": {
                    "inventoryAdjustmentGroup": {"createdAt": "2026-04-22T00:00:00Z"},
                    "userErrors": [],
                }
            },
        ]
    )
    out = tools["update_inventory"](
        inventory_item_id="42",
        location_id="9",
        quantity=0,
        confirm=True,
    )
    assert out.startswith("CONFIRMED —")
    assert "PREVIEW" not in out
    # Second call was the mutation
    mutation_query, mutation_vars = fc.calls[1]
    assert mutation_query == SET_INVENTORY
    assert mutation_vars == {
        "input": {
            "reason": "correction",
            "setQuantities": [
                {
                    "inventoryItemId": "gid://shopify/InventoryItem/42",
                    "locationId": "gid://shopify/Location/9",
                    "quantity": 0,
                }
            ],
        }
    }


# ---- update_inventory — quantity boundary ----


@pytest.mark.parametrize("quantity", [-1, 2_147_483_648])
def test_update_inventory_rejects_out_of_range_quantity(quantity):
    tools, fc = _build([])
    out = tools["update_inventory"](
        inventory_item_id="42",
        location_id="9",
        quantity=quantity,
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "quantity" in out
    assert len(fc.calls) == 0, "out-of-range quantity must reject before any Shopify call"


@pytest.mark.parametrize("quantity", [0, 2_147_483_647])
def test_update_inventory_accepts_boundary_quantity(quantity):
    tools, fc = _build(
        [
            _inventory_item_response(on_hand=5),
            {
                "inventorySetOnHandQuantities": {
                    "inventoryAdjustmentGroup": {"createdAt": "2026-04-22T00:00:00Z"},
                    "userErrors": [],
                }
            },
        ]
    )
    out = tools["update_inventory"](
        inventory_item_id="42",
        location_id="9",
        quantity=quantity,
        confirm=True,
    )
    assert out.startswith("CONFIRMED —")
    assert "PREVIEW" not in out


def test_update_inventory_handles_missing_inventory_item_without_crashing():
    """If the inventory_item_id doesn't exist (deleted / wrong id), Shopify
    returns {"inventoryItem": None}. The preview should render cleanly with
    'unknown' rather than crash on a None.get() chain."""
    tools, fc = _build([{"inventoryItem": None}])
    out = tools["update_inventory"](
        inventory_item_id="999999",
        location_id="9",
        quantity=5,
        confirm=False,
    )
    assert "PREVIEW" in out
    assert "Current on-hand   : unknown" in out
    assert "confirm=True" in out


def test_update_inventory_surfaces_user_errors_as_error_string():
    tools, fc = _build(
        [
            _inventory_item_response(on_hand=5),
            {
                "inventorySetOnHandQuantities": {
                    "inventoryAdjustmentGroup": None,
                    "userErrors": [
                        {
                            "field": ["input", "setQuantities", "0", "quantity"],
                            "message": "Quantity must be non-negative",
                        },
                    ],
                }
            },
        ]
    )
    out = tools["update_inventory"](
        inventory_item_id="42",
        location_id="9",
        quantity=5,
        confirm=True,
    )
    assert out.startswith(INJECTION_REMINDER + "Error:")
    assert "Quantity must be non-negative" in out


# ---- update_variant_inventory_tracking ----


def _tracked_update_ok(inv_item_gid, tracked):
    return {
        "inventoryItemUpdate": {
            "inventoryItem": {"id": inv_item_gid, "tracked": tracked},
            "userErrors": [],
        }
    }


def _tracked_update_err(field, message):
    return {
        "inventoryItemUpdate": {
            "inventoryItem": None,
            "userErrors": [{"field": field, "message": message}],
        }
    }


def test_tracking_product_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["update_variant_inventory_tracking"](
        product_id="999",
        tracked=True,
        confirm=True,
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
        product_id="555",
        tracked=True,
        confirm=False,
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
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_ok("gid://shopify/InventoryItem/100", True),
            _tracked_update_ok("gid://shopify/InventoryItem/101", True),
        ]
    )
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    # Story 9.24 round 2 (verifier finding): pin the exact first line so a
    # suffixed header (e.g. " (ok)") — which still satisfies `startswith` —
    # is caught here.
    assert out.split("\n", 1)[0] == "CONFIRMED — Variant inventory tracking update"
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


def test_tracking_transport_error_is_capped():
    # A mid-loop transport failure whose message is huge must be bounded in the
    # per-variant failure line, not echoed verbatim.
    variants = [_variant("100", "S", "REEF-S", [], tracked=False)]
    huge = "Q" * (REFLECT_MAX_LEN + 5000)
    tools, fc = _build(
        [
            _product_with_variants(variants),
            RuntimeError(huge),
        ]
    )
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=True,
    )
    assert "transport error:" in out
    assert "Q" * REFLECT_MAX_LEN in out
    assert "Q" * (REFLECT_MAX_LEN + 1) not in out


def test_tracking_unchanged_variants_get_no_mutation():
    """Variants already at the target state are reported as unchanged; no
    mutation is issued for them. Saves API calls on repeat runs."""
    variants = [
        _variant("100", "S", "REEF-S", [], tracked=True),  # already at target
        _variant("101", "M", "REEF-M", [], tracked=False),  # needs change
    ]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_ok("gid://shopify/InventoryItem/101", True),
        ]
    )
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=True,
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
        product_id="555",
        tracked=True,
        confirm=True,
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
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_ok("gid://shopify/InventoryItem/100", True),
        ]
    )
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
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_ok("gid://shopify/InventoryItem/100", True),
            _tracked_update_err(["input", "tracked"], "locked by another process"),
        ]
    )
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=True,
    )
    # Story 9.24: 1 changed / 1 failed is a partial write, not a confirmed one.
    assert out.startswith(
        INJECTION_REMINDER + "PARTIAL — Variant inventory tracking update (1 succeeded, 1 failed)"
    )
    assert "CONFIRMED" not in out
    assert "Changed (1):" in out
    assert "Failed (1):" in out
    # Full 'field: message' shape — locks in format_user_errors_joined output
    # so a regression in the helper surfaces here.
    assert "    • M: " + fenced("input.tracked: locked by another process") in out.splitlines()


def test_tracking_failed_bullet_renders_list_field_as_dotted_path():
    """Story 10.94: the per-variant Failed bullet embeds format_user_errors_joined
    with no "Error:" prefix; a `[String!]` path must render dotted there too."""
    variants = [_variant("101", "M", "REEF-M", [], tracked=False)]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_err(["input", "tracked"], "locked by another process"),
        ]
    )
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=True,
    )
    assert len(fc.calls) == 2, "the mutation must have run"
    assert "    • M: " + fenced("input.tracked: locked by another process") in out.splitlines()


def test_tracking_all_rejected_is_not_confirmed():
    """Story 9.24: every attempted variant is rejected — 0 succeeded, 2 failed
    — so the header must read FAILED, never CONFIRMED, though the per-variant
    Failed block is unchanged.

    Round 2 (verifier finding): an already-tracked variant is included so a
    mutant counting `unchanged` variants as `succeeded` is caught — without
    it, `succeeded = len(changed) + len(unchanged)` would read 1 (the
    unchanged variant) instead of 0 and the header would read PARTIAL
    instead of FAILED under that mutant, with no test noticing."""
    variants = [
        _variant("099", "XS", "REEF-XS", [], tracked=True),  # already at target
        _variant("100", "S", "REEF-S", [], tracked=False),
        _variant("101", "M", "REEF-M", [], tracked=False),
    ]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_err(["input", "tracked"], "locked by another process"),
            _tracked_update_err(["input", "tracked"], "locked by another process"),
        ]
    )
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=True,
    )
    assert out.startswith(INJECTION_REMINDER + "FAILED — Variant inventory tracking update")
    assert "CONFIRMED" not in out
    assert "Changed (0):" in out
    assert "Unchanged (1):" in out
    assert "Failed (2):" in out
    # Prove the write actually ran for both variants, not a vacuous pass.
    assert len(fc.calls) == 3


def test_tracking_all_variant_ids_unresolved_is_not_confirmed():
    """Story 9.24 round 3 (verifier finding): every requested variant id
    fails to RESOLVE, not just to mutate — `targets` is empty, so no
    mutation is ever attempted and `failed` (scoped to attempted-and-
    rejected variants) stays empty too. The original fix's mutation-only
    failed count read 0 for this case, so the header still read CONFIRMED
    over an "Unresolved variant ids:" block listing every id the caller
    asked for — the same hazard round 2 closed for publications. Nothing
    resolved, so this must read FAILED."""
    variants = [_variant("100", "S", "REEF-S", [], tracked=False)]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        variant_ids=["999"],
        confirm=True,
    )
    assert out.startswith("FAILED — Variant inventory tracking update")
    assert "CONFIRMED" not in out
    assert "Changed (0):" in out
    assert "999" in out[out.index("Unresolved variant ids:") :]
    # No mutation was possible — nothing resolved — but the product read did run.
    assert len(fc.calls) == 1
    assert fc.responses == []


def test_tracking_some_variant_ids_unresolved_success_reads_confirmed():
    """Story 9.24 round 4 (surviving mutant M5): one requested variant id
    resolves and its mutation succeeds, while another does not resolve at
    all. `targets` is non-empty (100 resolved), so the mutation-only
    `failed` count applies (round 3's "nothing resolved" fallback must NOT
    fire) — the unresolved id must not inflate the failed count. A mutant
    that summed `len(failed) + len(unresolved)` instead of the `if targets`
    branch would read failed_count=1 and PARTIAL instead of CONFIRMED; this
    must read CONFIRMED, with 999 still reported in the Unresolved block."""
    variants = [_variant("100", "S", "REEF-S", [], tracked=False)]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_ok("gid://shopify/InventoryItem/100", True),
        ]
    )
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        variant_ids=["100", "999"],
        confirm=True,
    )
    assert out.split("\n", 1)[0] == "CONFIRMED — Variant inventory tracking update"
    assert "999" in out[out.index("Unresolved variant ids:") :]
    assert len(fc.calls) == 2
    assert fc.responses == []


def test_tracking_unchanged_variant_alongside_unresolved_reads_confirmed():
    """Story 9.24 round 5 (surviving mutant M18): a requested variant that's
    already at the target tracked state needs no mutation at all — it lands
    in `unchanged`, not `to_change` — while a second requested id doesn't
    resolve. `targets` is non-empty (100 resolved), so the mutation-only
    `failed_count = len(failed) if targets else len(unresolved)` must read 0
    and the header must read CONFIRMED. A mutant keying that fallback on
    `to_change` instead of `targets` would see `to_change` empty here too
    (nothing needed changing) and wrongly fall back to `len(unresolved) = 1`,
    reading FAILED even though nothing was rejected. 999 must still be
    reported in the Unresolved block."""
    variants = [_variant("100", "S", "REEF-S", [], tracked=True)]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        variant_ids=["100", "999"],
        confirm=True,
    )
    assert out.split("\n", 1)[0] == "CONFIRMED — Variant inventory tracking update"
    assert "999" in out[out.index("Unresolved variant ids:") :]
    # No mutation was possible or needed (100 already matches) — prove the
    # read actually ran and nothing else was scripted or left unconsumed.
    assert len(fc.calls) == 1
    assert fc.responses == []


def test_tracking_preview_only_issues_exactly_one_execute_call():
    """confirm=False must not issue any mutations, even if there are targets."""
    variants = [_variant("100", "S", "REEF-S", [], tracked=False)]
    tools, fc = _build([_product_with_variants(variants)])
    tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=False,
    )
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_INVENTORY


def test_tracking_product_with_no_variants_issues_no_mutations():
    """Product exists but has zero variants — confirm is a clean no-op."""
    tools, fc = _build([_product_with_variants([])])
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=True,
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

    class FlakyFakeClient(FakeClient):
        def __init__(self) -> None:
            super().__init__([])

        def execute(self, query: str, variables: dict | None = None) -> object:
            self.calls.append((query, variables))
            if len(self.calls) == 1:
                return _product_with_variants(variants)
            if len(self.calls) == 3:
                # Variant 101's mutation throws
                raise RuntimeError("upstream 502")
            # Variants 100 and 102 succeed
            assert variables is not None
            return _tracked_update_ok(variables["id"], True)

    srv = CapturingServer()
    fc = FlakyFakeClient()
    inventory.register(srv, fc)
    out = srv.tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=True,
    )
    # Story 9.24: 2 changed / 1 failed is a partial write, not a confirmed one.
    assert out.startswith("PARTIAL — Variant inventory tracking update (2 succeeded, 1 failed)"), (
        out
    )
    assert "CONFIRMED" not in out
    assert "Changed (2):" in out
    assert "Failed (1):" in out
    assert "transport error" in out and "upstream 502" in out
    # All three mutations attempted (1 read + 3 mutation attempts)
    assert len(fc.calls) == 4


def test_tracking_unresolved_variant_ids_are_deduped():
    """A caller that supplies the same unknown id twice should see it reported
    once, not twice. Mirrors the dedup behavior in update_variant_inventory_policy."""
    variants = [_variant("100", "S", "REEF-S", [], tracked=False)]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_ok("gid://shopify/InventoryItem/100", True),
        ]
    )
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
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_ok("gid://shopify/InventoryItem/100", True),
        ]
    )
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
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _tracked_update_ok("gid://shopify/InventoryItem/102", True),
            _tracked_update_ok("gid://shopify/InventoryItem/100", True),
            _tracked_update_ok("gid://shopify/InventoryItem/101", True),
        ]
    )
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
    tools, fc = _build(
        [
            _product_with_variants([bad_variant, good_variant]),
            _tracked_update_ok("gid://shopify/InventoryItem/101", True),
        ]
    )
    out = tools["update_variant_inventory_tracking"](
        product_id="555",
        tracked=True,
        confirm=True,
    )
    # Story 9.24: 1 changed / 1 failed is a partial write, not a confirmed one.
    assert out.startswith("PARTIAL — Variant inventory tracking update (1 succeeded, 1 failed)")
    assert "CONFIRMED" not in out
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
        product_id="555",
        tracked=True,
        confirm=False,
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
    return {
        "inventorySetOnHandQuantities": {
            "inventoryAdjustmentGroup": {"createdAt": "2026-04-22T00:00:00Z"},
            "userErrors": [],
        }
    }


def _set_inventory_err(field, message):
    return {
        "inventorySetOnHandQuantities": {
            "inventoryAdjustmentGroup": None,
            "userErrors": [{"field": field, "message": message}],
        }
    }


def test_quantity_product_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["update_variant_inventory_quantity"](
        product_id="999",
        quantity=0,
        confirm=True,
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
        product_id="555",
        quantity=0,
        confirm=False,
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
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_ok(),
        ]
    )
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
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
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_ok(),
        ]
    )
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
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
        product_id="555",
        quantity=0,
        confirm=True,
    )
    # Story 9.25 pin: the genuine no-op keeps its exact text, byte for byte.
    assert out == (
        "CONFIRMED — Variant inventory quantity update (no-op)\n"
        "  Product     : Luminous Reef Tee (id: 555)\n"
        "  Target qty  : 0\n"
        "  Target loc  : all locations\n"
        "  Changed     : (none — nothing writable at target)\n"
        "  Unchanged (2):\n"
        "    • S @ Main — variant_id: 100, location: 9 — on-hand already at 0 "
        "(committed: 0, available: 0)\n"
        "    • M @ Main — variant_id: 101, location: 9 — on-hand already at 0 "
        "(committed: 0, available: 0)"
    ), out
    assert len(fc.calls) == 1


def test_quantity_successful_write_output_pins_exact_text():
    """Story 9.25 pin: the normal confirmed write keeps its exact text."""
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
        _variant("101", "M", "REEF-M", [_level(3, "gid://shopify/Location/9")]),
    ]
    tools, _fc = _build([_product_with_variants(variants), _set_inventory_ok()])
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
    )
    assert out == (
        "CONFIRMED — Variant inventory quantity update\n"
        "  Product     : Luminous Reef Tee (id: 555)\n"
        "  Target qty  : 0\n"
        "  Target loc  : all locations\n"
        "  Changed (2):\n"
        "    • S @ Main — variant_id: 100, location: 9 — on-hand 5 → 0 "
        "(committed: 0, available: 5)\n"
        "    • M @ Main — variant_id: 101, location: 9 — on-hand 3 → 0 "
        "(committed: 0, available: 3)\n"
        "  Unchanged (0):\n"
        "    (none)"
    ), out


def test_quantity_zero_out_writes_a_variant_whose_committed_units_hide_its_on_hand():
    """Story 10.111: on-hand 2 with 2 committed reads available 0. The write
    sets on-hand, so a target of 0 must write it, not call it "already at 0"."""
    variants = [_variant("100", "Butter / 4XL", "TB-4XL", [_level(2, committed=2)])]
    tools, fc = _build([_product_with_variants(variants), _set_inventory_ok()])
    preview = tools["update_variant_inventory_quantity"](product_id="555", quantity=0)
    assert (
        "  Would change (1):\n"
        "    • Butter / 4XL @ Main — variant_id: 100, location: 9 — on-hand 2 → 0 "
        "(committed: 2, available: 0)\n"
        "  Unchanged (0):\n"
    ) in preview, preview

    tools, fc = _build([_product_with_variants(variants), _set_inventory_ok()])
    out = tools["update_variant_inventory_quantity"](product_id="555", quantity=0, confirm=True)
    assert out.split("\n")[0] == "CONFIRMED — Variant inventory quantity update", out
    assert "  Changed (1):\n" in out, out
    assert [q for q, _ in fc.calls] == [GET_PRODUCT_INVENTORY, SET_INVENTORY]
    assert fc.calls[1][1] == {
        "input": {
            "reason": "correction",
            "setQuantities": [
                {
                    "inventoryItemId": "gid://shopify/InventoryItem/100",
                    "locationId": "gid://shopify/Location/9",
                    "quantity": 0,
                }
            ],
        }
    }


def test_quantity_target_equal_to_available_but_not_on_hand_is_written():
    """Story 10.111, the class in general: on-hand 5, committed 2, available 3,
    target 3. The target matches available only, so the pair needs a write."""
    variants = [_variant("100", "S", "REEF-S", [_level(5, committed=2)])]
    tools, fc = _build([_product_with_variants(variants), _set_inventory_ok()])
    out = tools["update_variant_inventory_quantity"](product_id="555", quantity=3, confirm=True)
    assert "on-hand 5 → 3 (committed: 2, available: 3)" in out, out
    assert fc.calls[1][0] == SET_INVENTORY
    assert fc.calls[1][1]["input"]["setQuantities"][0]["quantity"] == 3


def test_quantity_on_hand_at_target_with_committed_units_is_unchanged():
    """On-hand already equals the target, so no write, even though available
    differs (on-hand 3, committed 3, available 0, target 3)."""
    variants = [_variant("100", "S", "REEF-S", [_level(3, committed=3)])]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](product_id="555", quantity=3, confirm=True)
    assert out.split("\n")[0] == "CONFIRMED — Variant inventory quantity update (no-op)", out
    assert "— on-hand already at 3 (committed: 3, available: 0)" in out, out
    assert [q for q, _ in fc.calls] == [GET_PRODUCT_INVENTORY]


def test_quantity_missing_on_hand_counts_as_needs_change():
    """No on_hand entry: write it (the safer default), and show N/A."""
    level = {
        "quantities": [{"name": "available", "quantity": 0}],
        "location": {"id": "gid://shopify/Location/9", "name": "Main"},
    }
    variants = [_variant("100", "S", "REEF-S", [level])]
    tools, fc = _build([_product_with_variants(variants), _set_inventory_ok()])
    out = tools["update_variant_inventory_quantity"](product_id="555", quantity=0, confirm=True)
    assert "on-hand N/A → 0 (committed: N/A, available: 0)" in out, out
    assert fc.calls[1][0] == SET_INVENTORY


def test_quantity_all_variant_ids_unresolved_reads_failed_and_sends_no_mutation():
    """Story 9.25: every supplied variant id is unresolved, so nothing could be
    written. That reads FAILED, never CONFIRMED, and the Unresolved block stays."""
    variants = [_variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")])]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        variant_ids=["999"],
        confirm=True,
    )
    assert out == (
        "FAILED — Variant inventory quantity update\n"
        "  Product     : Luminous Reef Tee (id: 555)\n"
        "  Target qty  : 0\n"
        "  Target loc  : all locations\n"
        "  Changed     : (none — nothing writable at target)\n"
        "  Unchanged (0):\n"
        "    (none)\n"
        "  Unresolved variant ids:\n"
        "    • 999"
    ), out
    assert [q for q, _ in fc.calls] == [GET_PRODUCT_INVENTORY]


def test_quantity_unresolved_location_on_confirm_reads_failed_and_sends_no_mutation():
    """Story 9.25: a location_id that matches no level on any targeted variant
    leaves nothing to write. That reads FAILED, not a CONFIRMED no-op."""
    variants = [_variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")])]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        location_id="9999",
        confirm=True,
    )
    assert out.split("\n")[0] == "FAILED — Variant inventory quantity update", out
    assert out.endswith(
        "\n  Unresolved location: 9999 (not found on any of the targeted variants)"
    ), out
    assert [q for q, _ in fc.calls] == [GET_PRODUCT_INVENTORY]


def test_quantity_some_variant_ids_unresolved_and_rest_at_target_stays_confirmed_no_op():
    """Story 9.25: one id resolved and is already at target, one is a typo.
    Something requested resolved, so (like 9.24's tracking tool) this stays a
    CONFIRMED no-op, with the typo still listed."""
    variants = [_variant("100", "S", "REEF-S", [_level(0, "gid://shopify/Location/9")])]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        variant_ids=["100", "999"],
        confirm=True,
    )
    assert out.split("\n")[0] == "CONFIRMED — Variant inventory quantity update (no-op)", out
    assert out.endswith("\n  Unresolved variant ids:\n    • 999"), out
    assert len(fc.calls) == 1


def test_quantity_location_filter_narrows_to_one_location():
    """With location_id set, only levels at that location are considered."""
    variants = [
        _variant(
            "100",
            "S",
            "REEF-S",
            [
                _level(5, "gid://shopify/Location/9"),
                _level(2, "gid://shopify/Location/77"),
            ],
        ),
    ]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_ok(),
        ]
    )
    tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        location_id="9",
        confirm=True,
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
        product_id="555",
        quantity=0,
        location_id="9999",
        confirm=False,
    )
    assert "Unresolved location: 9999" in out
    assert "Would change (0):" in out


def test_quantity_variant_ids_filter_applies_and_unresolved_ids_surface():
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
        _variant("101", "M", "REEF-M", [_level(3, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_ok(),
        ]
    )
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        variant_ids=["100", "999"],
        confirm=True,
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
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_err(
                ["input", "setQuantities", "0", "quantity"],
                "Quantity must be non-negative",
            ),
        ]
    )
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
    )
    assert out.startswith(INJECTION_REMINDER + "Error:")
    assert "Quantity must be non-negative" in out


def test_quantity_user_errors_render_list_field_as_dotted_path():
    """Story 10.94: `field` is `[String!]`; the full error string must carry a
    dotted path, not a Python list repr."""
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_err(
                ["input", "setQuantities", "0", "quantity"],
                "Quantity must be non-negative",
            ),
        ]
    )
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
    )
    assert out == INJECTION_REMINDER + "Error: " + fenced(
        "input.setQuantities.0.quantity: Quantity must be non-negative"
    )


def test_quantity_variant_with_no_levels_contributes_no_pair():
    """A variant with inventoryLevels=[] has no (variant, location) pair, so
    it's not in `to_change` nor `unchanged` — just silently absent."""
    variants = [
        _variant("100", "S", "REEF-S", []),  # no levels at all
        _variant("101", "M", "REEF-M", [_level(3, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_ok(),
        ]
    )
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
    )
    # Only variant 101 contributes a pair.
    assert "Changed (1):" in out
    set_qtys = fc.calls[1][1]["input"]["setQuantities"]
    assert len(set_qtys) == 1
    assert set_qtys[0]["inventoryItemId"] == "gid://shopify/InventoryItem/101"


def test_quantity_multi_location_default_touches_every_variant_location_pair():
    """location_id omitted + variant with 2 levels → both pairs in the batch."""
    variants = [
        _variant(
            "100",
            "S",
            "REEF-S",
            [
                _level(5, "gid://shopify/Location/9"),
                _level(2, "gid://shopify/Location/77"),
            ],
        ),
    ]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_ok(),
        ]
    )
    tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
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
        product_id="555",
        quantity=0,
        confirm=False,
    )
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_INVENTORY


# ---- update_variant_inventory_quantity — quantity boundary ----


@pytest.mark.parametrize("quantity", [-1, 2_147_483_648])
def test_quantity_rejects_out_of_range_quantity(quantity):
    tools, fc = _build([])
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=quantity,
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "quantity" in out
    assert len(fc.calls) == 0, "out-of-range quantity must reject before any Shopify call"


@pytest.mark.parametrize("quantity", [0, 2_147_483_647])
def test_quantity_accepts_boundary_quantity(quantity):
    variants = [
        _variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/9")]),
    ]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_ok(),
        ]
    )
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=quantity,
        confirm=True,
    )
    assert out.startswith("CONFIRMED")


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
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_ok(),
        ]
    )
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
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
                "inventoryLevels": {
                    "nodes": [
                        {
                            "quantities": [{"name": "available", "quantity": 3}],
                            "location": None,
                        }
                    ]
                },
            },
        },
    ]
    tools, fc = _build(
        [
            _product_with_variants(variants),
            _set_inventory_ok(),
        ]
    )
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
    )
    # Story 9.25: one pair written and one needed change skipped reads
    # PARTIAL, the same as 9.24's tracking tool for a missing inventoryItem id.
    assert out.split("\n")[0] == (
        "PARTIAL — Variant inventory quantity update (1 succeeded, 1 failed)"
    ), out
    assert "Changed (1):" in out
    assert "Skipped (1):" in out
    assert "missing inventoryItem.id or location.id" in out
    # Only the healthy variant is in the batch
    set_qtys = fc.calls[1][1]["input"]["setQuantities"]
    assert len(set_qtys) == 1
    assert set_qtys[0]["inventoryItemId"] == "gid://shopify/InventoryItem/100"


def _variant_with_null_location_level(vid, title, available):
    """A variant whose only level has location=None — every pair that needs
    a change for it lands in `skipped`."""
    return {
        "id": f"gid://shopify/ProductVariant/{vid}",
        "title": title,
        "sku": f"SKU-{vid}",
        "inventoryItem": {
            "id": f"gid://shopify/InventoryItem/{vid}",
            "tracked": True,
            "inventoryLevels": {
                "nodes": [
                    {"quantities": [{"name": "available", "quantity": available}], "location": None}
                ]
            },
        },
    }


def test_quantity_every_needed_change_skipped_reads_failed_and_sends_no_mutation():
    """Story 9.25: every pair that needs a change is skipped, so nothing was
    writable. That reads FAILED (not the genuine-no-op CONFIRMED text), the
    Skipped block stays, and no mutation is sent."""
    variants = [_variant_with_null_location_level("101", "Broken", 3)]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
    )
    assert out.split("\n")[0] == "FAILED — Variant inventory quantity update", out
    assert "  Changed     : (none — nothing writable at target)\n" in out
    assert "\n  Skipped (1):\n" in out
    assert "missing inventoryItem.id or location.id, not written" in out
    assert [q for q, _ in fc.calls] == [GET_PRODUCT_INVENTORY]


def test_quantity_resolved_variant_without_levels_plus_typo_stays_confirmed_no_op():
    """Story 9.25 review: a variant that resolves but has no inventory levels,
    plus one unresolved id. Something requested resolved, so (keyed on the
    resolved variants, like 9.24's tracking tool) this stays a CONFIRMED no-op
    — one typo must not flip it to FAILED."""
    variants = [_variant("100", "S", "REEF-S", [])]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        variant_ids=["100", "999"],
        confirm=True,
    )
    assert out.split("\n")[0] == "CONFIRMED — Variant inventory quantity update (no-op)", out
    assert out.endswith("\n  Unresolved variant ids:\n    • 999"), out
    assert len(fc.calls) == 1


def test_quantity_skipped_pair_plus_typo_reads_failed():
    """Story 9.25 review: the only resolved variant's needed change is skipped
    and the other id is a typo. Nothing requested was written, so FAILED."""
    variants = [_variant_with_null_location_level("101", "Broken", 3)]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        variant_ids=["101", "999"],
        confirm=True,
    )
    assert out.split("\n")[0] == "FAILED — Variant inventory quantity update", out
    assert "\n  Skipped (1):\n" in out
    assert out.endswith("\n  Unresolved variant ids:\n    • 999"), out
    assert len(fc.calls) == 1


def test_quantity_skipped_pair_plus_unchanged_pair_reads_failed():
    """Story 9.25 review: one pair is already at target and the other needed
    a change but was skipped. Nothing that needed a change landed, so FAILED."""
    variants = [
        _variant("100", "S", "REEF-S", [_level(0, "gid://shopify/Location/9")]),
        _variant_with_null_location_level("101", "Broken", 3),
    ]
    tools, fc = _build([_product_with_variants(variants)])
    out = tools["update_variant_inventory_quantity"](
        product_id="555",
        quantity=0,
        confirm=True,
    )
    assert out.split("\n")[0] == "FAILED — Variant inventory quantity update", out
    assert "  Unchanged (1):\n" in out
    assert "\n  Skipped (1):\n" in out
    assert len(fc.calls) == 1
