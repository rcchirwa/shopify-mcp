"""
Offline unit tests for tools/_resolvers.py.

Covers resolve_variant_ids_with_variants (Story 9.3): in-memory resolution
of numeric IDs, GIDs, and SKUs against a pre-fetched variants list.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_resolvers.py -v
"""

import pytest

from shopify_mcp.tools._resolvers import (
    resolve_variant_ids_with_variants,
)

PRODUCT_GID = "gid://shopify/Product/5234567890"


# ---------- resolve_variant_ids_with_variants (in-memory, no fetch) ----------


def test_with_variants_numeric_short_circuits_ignoring_variants_list():
    # Numeric inputs don't consult the variants list at all — Story 9.3's
    # pricing tool relies on this to do its own product-existence check.
    out = resolve_variant_ids_with_variants(["42"], variants=[], product_gid=PRODUCT_GID)
    assert out == ["gid://shopify/ProductVariant/42"]


def test_with_variants_sku_resolves_against_supplied_list():
    out = resolve_variant_ids_with_variants(
        ["TARGET"],
        variants=[
            {"id": "gid://shopify/ProductVariant/1", "sku": "OTHER"},
            {"id": "gid://shopify/ProductVariant/2", "sku": "TARGET"},
        ],
        product_gid=PRODUCT_GID,
    )
    assert out == ["gid://shopify/ProductVariant/2"]


def test_with_variants_sku_not_in_list_raises():
    with pytest.raises(ValueError) as exc:
        resolve_variant_ids_with_variants(
            ["MISSING"],
            variants=[{"id": "gid://shopify/ProductVariant/1", "sku": "OTHER"}],
            product_gid=PRODUCT_GID,
        )
    assert "MISSING" in str(exc.value)


def test_with_variants_ambiguous_sku_in_list_raises():
    with pytest.raises(ValueError) as exc:
        resolve_variant_ids_with_variants(
            ["DUP"],
            variants=[
                {"id": "gid://shopify/ProductVariant/1", "sku": "DUP"},
                {"id": "gid://shopify/ProductVariant/2", "sku": "DUP"},
            ],
            product_gid=PRODUCT_GID,
        )
    assert "multiple variants" in str(exc.value)


def test_with_variants_validates_inputs_before_consulting_list():
    # Empty string is rejected up front, regardless of what's in variants.
    with pytest.raises(ValueError):
        resolve_variant_ids_with_variants(
            [""],
            variants=[{"id": "gid://shopify/ProductVariant/1", "sku": "ANY"}],
            product_gid=PRODUCT_GID,
        )


def test_with_variants_non_string_id_raises():
    # _validate_variant_id rejects non-string inputs (line 19 coverage).
    with pytest.raises(ValueError):
        resolve_variant_ids_with_variants(
            [42],  # type: ignore[list-item]
            variants=[],
            product_gid=PRODUCT_GID,
        )


def test_with_variants_empty_tail_gid_raises():
    # _classify_no_fetch rejects a GID with no numeric tail (line 34 coverage).
    with pytest.raises(ValueError, match="Malformed variant GID"):
        resolve_variant_ids_with_variants(
            ["gid://shopify/ProductVariant/"],
            variants=[],
            product_gid=PRODUCT_GID,
        )


# ---------------------------------------------------------------------------
# Story 10.66 (T-9.5-unicode-digits) — `_classify_no_fetch`'s bare
# `stripped.isdigit()` was Unicode-aware, so non-ASCII digits wrapped into a
# malformed GID (`gid://shopify/ProductVariant/²`) instead of falling through
# to the SKU-lookup path. Mirrors Story 10.64's fix at
# `_product_resolver.py::_resolve_product`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "²",  # superscript two
        "\uff12\uff10",  # fullwidth 20, escaped to satisfy ruff RUF001
        "٤٢",  # Arabic-Indic 42
    ],
)
def test_with_variants_non_ascii_digits_do_not_wrap_into_a_gid(value):
    # Falls through to the SKU-lookup path instead of `to_gid`; since it
    # isn't a real SKU on the (empty) variants list, that lookup raises with
    # the raw value in the message rather than silently succeeding on a
    # malformed GID.
    with pytest.raises(ValueError) as exc:
        resolve_variant_ids_with_variants([value], variants=[], product_gid=PRODUCT_GID)
    assert value in str(exc.value)
    assert "No variant on product" in str(exc.value)


def test_with_variants_mixed_inputs_preserve_order():
    out = resolve_variant_ids_with_variants(
        ["SKU-A", "42", "gid://shopify/ProductVariant/9", "SKU-B"],
        variants=[
            {"id": "gid://shopify/ProductVariant/100", "sku": "SKU-A"},
            {"id": "gid://shopify/ProductVariant/200", "sku": "SKU-B"},
        ],
        product_gid=PRODUCT_GID,
    )
    assert out == [
        "gid://shopify/ProductVariant/100",
        "gid://shopify/ProductVariant/42",
        "gid://shopify/ProductVariant/9",
        "gid://shopify/ProductVariant/200",
    ]
