"""
Offline unit tests for tools/_resolvers.py.

Covers all six paths called out in Trello card ualcSqFq (Story 9.0 AC #4):
numeric ID, GID passthrough, SKU unique, SKU ambiguous, SKU not found,
malformed input.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_resolvers_offline.py -v
"""

import pytest

from _testing import FakeClient
from tools._resolvers import (
    GET_PRODUCT_VARIANTS_FOR_RESOLVE,
    resolve_variant_id_to_gid,
    resolve_variant_ids_to_gids,
)

PRODUCT_GID = "gid://shopify/Product/5234567890"


def _variants_response(*variants: tuple[str, str]) -> dict:
    return {
        "product": {
            "id": PRODUCT_GID,
            "variants": {"nodes": [{"id": gid, "sku": sku} for gid, sku in variants]},
        }
    }


def test_numeric_id_short_circuits_without_network():
    fc = FakeClient([])
    out = resolve_variant_id_to_gid(fc, PRODUCT_GID, "42")
    assert out == "gid://shopify/ProductVariant/42"
    assert fc.calls == []


def test_gid_passthrough_without_network():
    fc = FakeClient([])
    gid = "gid://shopify/ProductVariant/99"
    out = resolve_variant_id_to_gid(fc, PRODUCT_GID, gid)
    assert out == gid
    assert fc.calls == []


def test_sku_unique_match_returns_variant_gid():
    fc = FakeClient(
        [
            _variants_response(
                ("gid://shopify/ProductVariant/1", "VBLC-SM"),
                ("gid://shopify/ProductVariant/2", "VBLC-MD"),
                ("gid://shopify/ProductVariant/3", "VBLC-LG"),
            )
        ]
    )
    out = resolve_variant_id_to_gid(fc, PRODUCT_GID, "VBLC-MD")
    assert out == "gid://shopify/ProductVariant/2"
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_VARIANTS_FOR_RESOLVE
    assert fc.calls[0][1] == {"id": PRODUCT_GID}


def test_sku_ambiguous_match_raises_with_all_gids():
    fc = FakeClient(
        [
            _variants_response(
                ("gid://shopify/ProductVariant/1", "DUP"),
                ("gid://shopify/ProductVariant/2", "DUP"),
            )
        ]
    )
    with pytest.raises(ValueError) as exc:
        resolve_variant_id_to_gid(fc, PRODUCT_GID, "DUP")
    msg = str(exc.value)
    assert "DUP" in msg
    assert "gid://shopify/ProductVariant/1" in msg
    assert "gid://shopify/ProductVariant/2" in msg


def test_sku_not_found_raises_with_sku_echoed():
    fc = FakeClient(
        [
            _variants_response(
                ("gid://shopify/ProductVariant/1", "OTHER"),
            )
        ]
    )
    with pytest.raises(ValueError) as exc:
        resolve_variant_id_to_gid(fc, PRODUCT_GID, "MISSING-SKU")
    assert "MISSING-SKU" in str(exc.value)


def test_empty_string_raises():
    fc = FakeClient([])
    with pytest.raises(ValueError):
        resolve_variant_id_to_gid(fc, PRODUCT_GID, "")
    assert fc.calls == []


def test_whitespace_only_raises():
    fc = FakeClient([])
    with pytest.raises(ValueError):
        resolve_variant_id_to_gid(fc, PRODUCT_GID, "   ")
    assert fc.calls == []


def test_non_string_raises():
    fc = FakeClient([])
    with pytest.raises(ValueError):
        resolve_variant_id_to_gid(fc, PRODUCT_GID, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        resolve_variant_id_to_gid(fc, PRODUCT_GID, 42)  # type: ignore[arg-type]
    assert fc.calls == []


def test_sku_match_ignores_variants_with_no_sku():
    fc = FakeClient(
        [
            _variants_response(
                ("gid://shopify/ProductVariant/1", ""),
                ("gid://shopify/ProductVariant/2", "TARGET"),
            )
        ]
    )
    out = resolve_variant_id_to_gid(fc, PRODUCT_GID, "TARGET")
    assert out == "gid://shopify/ProductVariant/2"


def test_sku_with_surrounding_whitespace_is_trimmed_before_match():
    fc = FakeClient(
        [
            _variants_response(("gid://shopify/ProductVariant/7", "TRIMMED")),
        ]
    )
    out = resolve_variant_id_to_gid(fc, PRODUCT_GID, "  TRIMMED  ")
    assert out == "gid://shopify/ProductVariant/7"


def test_sku_match_skips_variants_with_null_sku():
    # Shopify returns sku=null (not "") for variants without an assigned SKU.
    fc = FakeClient(
        [
            {
                "product": {
                    "variants": {
                        "nodes": [
                            {"id": "gid://shopify/ProductVariant/1", "sku": None},
                            {"id": "gid://shopify/ProductVariant/2", "sku": "TARGET"},
                        ]
                    },
                }
            }
        ]
    )
    out = resolve_variant_id_to_gid(fc, PRODUCT_GID, "TARGET")
    assert out == "gid://shopify/ProductVariant/2"


def test_product_not_found_on_sku_path_raises_distinct_error():
    # SKU path with a non-existent product — must NOT collapse into "SKU not
    # found on product." The two failure modes are distinct for callers.
    fc = FakeClient([{"product": None}])
    with pytest.raises(ValueError) as exc:
        resolve_variant_id_to_gid(fc, PRODUCT_GID, "ANY-SKU")
    msg = str(exc.value)
    assert "Product not found" in msg
    assert PRODUCT_GID in msg


def test_empty_tail_variant_gid_rejected_without_network():
    fc = FakeClient([])
    with pytest.raises(ValueError) as exc:
        resolve_variant_id_to_gid(fc, PRODUCT_GID, "gid://shopify/ProductVariant/")
    assert "Malformed variant GID" in str(exc.value)
    assert fc.calls == []


# ---------- resolve_variant_ids_to_gids (batch) ----------


def test_batch_resolve_all_gids_makes_no_network_call():
    fc = FakeClient([])
    out = resolve_variant_ids_to_gids(
        fc,
        PRODUCT_GID,
        [
            "gid://shopify/ProductVariant/1",
            "gid://shopify/ProductVariant/2",
        ],
    )
    assert out == [
        "gid://shopify/ProductVariant/1",
        "gid://shopify/ProductVariant/2",
    ]
    assert fc.calls == []


def test_batch_resolve_all_numeric_makes_no_network_call():
    fc = FakeClient([])
    out = resolve_variant_ids_to_gids(fc, PRODUCT_GID, ["10", "20", "30"])
    assert out == [
        "gid://shopify/ProductVariant/10",
        "gid://shopify/ProductVariant/20",
        "gid://shopify/ProductVariant/30",
    ]
    assert fc.calls == []


def test_batch_resolve_mixed_inputs_uses_single_query_and_preserves_order():
    fc = FakeClient(
        [
            _variants_response(
                ("gid://shopify/ProductVariant/100", "SKU-A"),
                ("gid://shopify/ProductVariant/200", "SKU-B"),
                ("gid://shopify/ProductVariant/300", "SKU-C"),
            )
        ]
    )
    out = resolve_variant_ids_to_gids(
        fc,
        PRODUCT_GID,
        [
            "SKU-B",
            "gid://shopify/ProductVariant/999",
            "SKU-A",
            "42",
            "SKU-C",
        ],
    )
    assert out == [
        "gid://shopify/ProductVariant/200",
        "gid://shopify/ProductVariant/999",
        "gid://shopify/ProductVariant/100",
        "gid://shopify/ProductVariant/42",
        "gid://shopify/ProductVariant/300",
    ]
    # Critical: only ONE fetch for the three SKU entries (the perf point).
    assert len(fc.calls) == 1


def test_batch_resolve_one_missing_sku_raises_without_partial_result():
    fc = FakeClient(
        [
            _variants_response(
                ("gid://shopify/ProductVariant/1", "GOOD"),
            )
        ]
    )
    with pytest.raises(ValueError) as exc:
        resolve_variant_ids_to_gids(fc, PRODUCT_GID, ["GOOD", "MISSING"])
    assert "MISSING" in str(exc.value)


def test_batch_resolve_one_ambiguous_sku_raises():
    fc = FakeClient(
        [
            _variants_response(
                ("gid://shopify/ProductVariant/1", "DUP"),
                ("gid://shopify/ProductVariant/2", "DUP"),
            )
        ]
    )
    with pytest.raises(ValueError) as exc:
        resolve_variant_ids_to_gids(fc, PRODUCT_GID, ["DUP"])
    assert "multiple variants" in str(exc.value)


def test_batch_resolve_validates_all_inputs_before_fetching():
    # A malformed entry buried in the middle of the list should be caught up
    # front, before the network call fires.
    fc = FakeClient([])
    with pytest.raises(ValueError):
        resolve_variant_ids_to_gids(fc, PRODUCT_GID, ["SKU-A", "", "SKU-B"])
    assert fc.calls == []


def test_batch_resolve_empty_list_returns_empty_list_no_network():
    fc = FakeClient([])
    out = resolve_variant_ids_to_gids(fc, PRODUCT_GID, [])
    assert out == []
    assert fc.calls == []
