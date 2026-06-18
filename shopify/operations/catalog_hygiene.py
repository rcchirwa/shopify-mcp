"""Typed catalog-hygiene operations — data access over ``shopify.queries.catalog_hygiene``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs the input-building + query/mutation execution, returning the raw
Shopify response. No MCP imports and no output formatting, so these are callable
from non-MCP entry points (CLI, scripts, tests) — the four metafield/options
operations the ``/vanish-catalog-hygiene`` skill leans on among them (Story 10.25
/ A5, AC4). ``tools/catalog_hygiene.py`` layers param coercion, GID resolution,
the preview/confirm flow, and string formatting on top.

GID coercion: the per-tool resolvers in ``tools/catalog_hygiene.py`` (handle →
GID via ``read_product_by_handle_min`` / ``read_product_snapshot_by_*``) hand a
fully-resolved Product GID to the read/mutation operations here, which build the
``{"id": ...}`` / nested-input variable dicts and execute.
"""

from typing import Any

from shopify._client import GraphQLClient
from shopify.queries.catalog_hygiene import (
    GET_PRODUCT_BY_HANDLE_MIN,
    GET_PRODUCT_CATEGORY,
    GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA,
    GET_PRODUCT_MEDIA_PAGE,
    GET_PRODUCT_VARIANTS_FOR_PRICING,
    METAFIELDS_DELETE_MUTATION,
    METAFIELDS_SET_MUTATION,
    PRODUCT_VARIANT_APPEND_MEDIA,
    PRODUCT_VARIANT_DETACH_MEDIA,
    TAXONOMY_SEARCH,
    UPDATE_PRODUCT_CATEGORY,
    UPDATE_PRODUCT_OPTION,
    UPDATE_PRODUCT_TYPE,
    UPDATE_PRODUCT_VARIANTS_PRICING,
    UPDATE_PRODUCT_VENDOR,
    _build_batch_resolve_query,
)

# ---------- reads (return the raw Shopify response dict) ----------


def read_variants_for_pricing(client: GraphQLClient, product_gid: str) -> dict[str, Any]:
    """Read a product's variants (id/sku/price/compareAtPrice) for the pricing tool."""
    return client.execute(GET_PRODUCT_VARIANTS_FOR_PRICING, {"id": product_gid})


def read_product_category(client: GraphQLClient, product_gid: str) -> dict[str, Any]:
    """Read a product's current category (idempotency check + post-write snapshot)."""
    return client.execute(GET_PRODUCT_CATEGORY, {"id": product_gid})


def read_product_by_handle_min(client: GraphQLClient, handle: str) -> dict[str, Any]:
    """Resolve a product handle to its id (minimal read)."""
    return client.execute(GET_PRODUCT_BY_HANDLE_MIN, {"handle": handle})


def search_taxonomy_categories(client: GraphQLClient, search: str) -> dict[str, Any]:
    """Run Shopify's Standard Product Taxonomy search for a free-text term."""
    return client.execute(TAXONOMY_SEARCH, {"search": search})


def read_product_snapshot_by_id(
    client: GraphQLClient, query: str, product_gid: str
) -> dict[str, Any]:
    """Read a product snapshot by GID using the supplied by-id query.

    The vendor / type / options tools each pass their own narrow read query;
    the operation only owns input-building + execution.
    """
    return client.execute(query, {"id": product_gid})


def read_product_snapshot_by_handle(
    client: GraphQLClient, query: str, handle: str
) -> dict[str, Any]:
    """Read a product snapshot by handle using the supplied by-handle query."""
    return client.execute(query, {"handle": handle})


def read_product_media_and_variant_media(
    client: GraphQLClient,
    product_gid: str,
    *,
    media_first: int,
    media_after: str | None,
) -> dict[str, Any]:
    """Read product media + per-variant bound media in one round-trip (first page)."""
    return client.execute(
        GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA,
        {"id": product_gid, "mediaFirst": media_first, "mediaAfter": media_after},
    )


def read_product_media_page(
    client: GraphQLClient,
    product_gid: str,
    *,
    media_first: int,
    media_after: str,
) -> dict[str, Any]:
    """Read a continuation page of product media (page 2+ of the media connection)."""
    return client.execute(
        GET_PRODUCT_MEDIA_PAGE,
        {"id": product_gid, "mediaFirst": media_first, "mediaAfter": media_after},
    )


def read_product_metafields_page(
    client: GraphQLClient,
    *,
    query: str,
    product_gid: str,
    page_size: int,
    filter_mode: str,
    ns_filter: str | None,
    keys_filter: list[str] | None,
    fetch_metafields: bool,
    metafields_cursor: str | None,
    fetch_variants: bool,
    variants_page_size: int,
    variants_cursor: str | None,
) -> dict[str, Any]:
    """Execute one page of the get_product_metafields paginated read.

    Builds the per-iteration variables dict exactly as the connection state
    requires — the ``filter_mode`` arg (keys / namespace / none) and the
    fetch flags decide which optional variables are declared — then executes
    the caller-selected query (combined / metafields-only / variants-only).
    """
    variables: dict[str, Any] = {"id": product_gid, "first": page_size}
    if filter_mode == "keys":
        variables["keys"] = keys_filter
    elif filter_mode == "namespace":
        variables["namespace"] = ns_filter
    if fetch_metafields:
        variables["after"] = metafields_cursor
    if fetch_variants:
        variables["variantsFirst"] = variants_page_size
        variables["variantsAfter"] = variants_cursor
    return client.execute(query, variables)


def resolve_metafields_batch(
    client: GraphQLClient, classified: list[dict[str, Any]]
) -> dict[str, Any]:
    """Resolve every classified delete-entry's metafield identity in one round-trip.

    Builds the aliased ``node(id:)`` batch query from ``classified`` and
    executes it; the caller parses the per-alias (``e0``, ``e1``, …) response.
    """
    batch_query, batch_vars = _build_batch_resolve_query(classified)
    return client.execute(batch_query, batch_vars)


# ---------- writes (return the raw mutation result) ----------


def update_variants_pricing(
    client: GraphQLClient, product_gid: str, variants_input: list[dict[str, Any]]
) -> dict[str, Any]:
    """Execute a productVariantsBulkUpdate setting price/compareAtPrice per variant."""
    return client.execute(
        UPDATE_PRODUCT_VARIANTS_PRICING,
        {"productId": product_gid, "variants": variants_input},
    )


def update_product_category(
    client: GraphQLClient, product_gid: str, category_gid: str
) -> dict[str, Any]:
    """Execute a productUpdate setting the Standard Taxonomy category."""
    return client.execute(
        UPDATE_PRODUCT_CATEGORY,
        {"product": {"id": product_gid, "category": category_gid}},
    )


def update_product_vendor(
    client: GraphQLClient, product_gid: str, vendor: str | None
) -> dict[str, Any]:
    """Execute a productUpdate setting (or clearing, via ``None``) the vendor."""
    return client.execute(
        UPDATE_PRODUCT_VENDOR,
        {"product": {"id": product_gid, "vendor": vendor}},
    )


def update_product_type(
    client: GraphQLClient, product_gid: str, product_type: str
) -> dict[str, Any]:
    """Execute a productUpdate setting the legacy free-text productType."""
    return client.execute(
        UPDATE_PRODUCT_TYPE,
        {"product": {"id": product_gid, "productType": product_type}},
    )


def detach_variant_media(
    client: GraphQLClient, product_gid: str, variant_media: list[dict[str, Any]]
) -> dict[str, Any]:
    """Execute a productVariantDetachMedia clearing the supplied variant bindings."""
    return client.execute(
        PRODUCT_VARIANT_DETACH_MEDIA,
        {"productId": product_gid, "variantMedia": variant_media},
    )


def append_variant_media(
    client: GraphQLClient, product_gid: str, variant_media: list[dict[str, Any]]
) -> dict[str, Any]:
    """Execute a productVariantAppendMedia binding the supplied media to variants."""
    return client.execute(
        PRODUCT_VARIANT_APPEND_MEDIA,
        {"productId": product_gid, "variantMedia": variant_media},
    )


def set_metafields(client: GraphQLClient, mutation_input: list[dict[str, Any]]) -> dict[str, Any]:
    """Execute a metafieldsSet with the supplied (already-normalized) entries."""
    return client.execute(METAFIELDS_SET_MUTATION, {"metafields": mutation_input})


def delete_metafields(
    client: GraphQLClient, mutation_input: list[dict[str, Any]]
) -> dict[str, Any]:
    """Execute a metafieldsDelete with the supplied owner/namespace/key identifiers."""
    return client.execute(METAFIELDS_DELETE_MUTATION, {"metafields": mutation_input})


def update_product_option(
    client: GraphQLClient,
    product_gid: str,
    option_input: dict[str, Any],
    option_values_to_update: list[dict[str, Any]],
    variant_strategy: str,
) -> dict[str, Any]:
    """Execute a productOptionUpdate renaming an option and/or its option values."""
    return client.execute(
        UPDATE_PRODUCT_OPTION,
        {
            "productId": product_gid,
            "option": option_input,
            "optionValuesToUpdate": option_values_to_update,
            "variantStrategy": variant_strategy,
        },
    )
