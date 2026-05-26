"""
Shared identifier resolvers reused across catalog-hygiene tools.

Story 9.3 added `resolve_variant_ids_with_variants` — same resolution
semantics but takes a pre-fetched variants list instead of fetching. Use
when the caller already needs a wider read (e.g. pricing tool wants
price + compareAtPrice in the same round-trip).
"""

from typing import Any

from tools._gid import to_gid

_VARIANT_GID_PREFIX = "gid://shopify/ProductVariant/"


def _validate_variant_id(variant_id: object) -> str:
    if not isinstance(variant_id, str):
        raise ValueError("variant_id must be a non-empty string")
    stripped = variant_id.strip()
    if not stripped:
        raise ValueError("variant_id must be a non-empty string")
    return stripped


def _classify_no_fetch(stripped: str) -> str | None:
    """Return a resolved GID if `stripped` can be handled without a network call.

    Returns None when the caller has to look the value up by SKU.
    Raises ValueError when the input is a malformed variant GID.
    """
    if stripped.startswith(_VARIANT_GID_PREFIX):
        if not stripped[len(_VARIANT_GID_PREFIX) :]:
            raise ValueError(f"Malformed variant GID (empty tail): {stripped!r}")
        return stripped
    if stripped.isdigit():
        return to_gid("ProductVariant", stripped)
    return None


def resolve_variant_ids_with_variants(
    variant_ids: list[str],
    variants: list[dict[str, Any]],
    *,
    product_gid: str,
) -> list[str]:
    """Resolve variant identifiers using a pre-fetched variants list.

    No network call — the caller has already fetched the variants for some
    other purpose (e.g. catalog_hygiene's pricing tool reads price + sku +
    compareAtPrice in one round-trip and feeds the result here).

    `variants` is a list of `{id, sku, ...}` dicts; extra fields are
    ignored. `product_gid` is used only in error messages.

    Raises ValueError on malformed input, an empty-tail variant GID,
    unknown SKU, or ambiguous SKU.
    """
    stripped_ids = [_validate_variant_id(v) for v in variant_ids]

    resolved: list[str | None] = []
    needs_sku_lookup = False
    for stripped in stripped_ids:
        no_fetch = _classify_no_fetch(stripped)
        resolved.append(no_fetch)
        if no_fetch is None:
            needs_sku_lookup = True

    if not needs_sku_lookup:
        return [r for r in resolved if r is not None]

    # Pre-index by SKU once so a batch with M SKU entries stays O(N+M) instead of O(N*M).
    sku_index: dict[str, list[str]] = {}
    for node in variants:
        sku = node.get("sku") or ""
        if sku:
            sku_index.setdefault(sku, []).append(node["id"])

    for i, stripped in enumerate(stripped_ids):
        if resolved[i] is not None:
            continue
        matches = sku_index.get(stripped, [])
        if not matches:
            raise ValueError(f"No variant on product {product_gid} with SKU {stripped!r}")
        if len(matches) > 1:
            gids = ", ".join(matches)
            raise ValueError(
                f"SKU {stripped!r} matches multiple variants on product {product_gid}: {gids}"
            )
        resolved[i] = matches[0]

    return [r for r in resolved if r is not None]
