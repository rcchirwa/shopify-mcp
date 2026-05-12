"""
Shared identifier resolvers reused across catalog-hygiene tools.

Story 9.0 ships `resolve_variant_id_to_gid` (single) and
`resolve_variant_ids_to_gids` (batch) so Stories 9.3 (pricing) and 9.6
(variant image binding) don't each invent a SKU lookup. The batch helper
folds N SKU resolutions into one round-trip; tools iterating `variants[]`
should prefer it over a loop of single calls.
"""

from shopify_client import ShopifyClient, to_gid

# Narrower than GET_PRODUCT_FULL_BY_ID — only what's needed to map SKU → variant GID.
# Cap of 250 matches Shopify's `productVariantsBulkUpdate` window; products with
# more variants would need pagination, which Story 9.0 does not implement.
GET_PRODUCT_VARIANTS_FOR_RESOLVE = """
query GetProductVariantsForResolve($id: ID!) {
  product(id: $id) {
    variants(first: 250) {
      nodes { id sku }
    }
  }
}
"""

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


def resolve_variant_ids_to_gids(
    client: ShopifyClient,
    product_gid: str,
    variant_ids: list[str],
) -> list[str]:
    """Batch-resolve variant identifiers (numeric / GID / SKU) to Variant GIDs.

    Mixed inputs are supported. Numeric and GID entries short-circuit without
    a fetch; SKU entries trigger a single product-variants query whose result
    is reused across every SKU in the batch. Order in the returned list mirrors
    the input list.

    Raises ValueError on malformed input, an empty-tail variant GID, unknown
    SKU, ambiguous SKU (>1 variant on the product with the same SKU), or
    when `product_gid` doesn't exist on Shopify.
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

    data = client.execute(GET_PRODUCT_VARIANTS_FOR_RESOLVE, {"id": product_gid})
    product = (data or {}).get("product")
    if product is None:
        raise ValueError(f"Product not found: {product_gid}")
    nodes = (product.get("variants") or {}).get("nodes") or []

    # Pre-index by SKU once so a batch with M SKU entries stays O(N+M) instead of O(N*M).
    sku_index: dict[str, list[str]] = {}
    for node in nodes:
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


def resolve_variant_id_to_gid(
    client: ShopifyClient,
    product_gid: str,
    variant_id: str,
) -> str:
    """Resolve a single variant identifier (numeric / GID / SKU) to a Variant GID.

    Thin wrapper over `resolve_variant_ids_to_gids` — prefer the batch helper
    when iterating over `variants[]` to fold N round-trips into one.
    """
    return resolve_variant_ids_to_gids(client, product_gid, [variant_id])[0]
