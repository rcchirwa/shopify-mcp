"""Typed product operations — business logic over ``shopify.queries.products``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs GID coercion + query/mutation execution, returning structured data.
No MCP imports and no output formatting, so these are callable from non-MCP
entry points (CLI, scripts, tests). ``tools/products.py`` layers param coercion,
the preview/confirm flow, and string formatting on top.
"""

from typing import Any

from shopify_mcp.shopify._client import GraphQLClient
from shopify_mcp.shopify._identifiers import reject_both_identifiers
from shopify_mcp.shopify._ids import to_gid
from shopify_mcp.shopify.queries.products import (
    GET_PRODUCT_BY_HANDLE,
    GET_PRODUCT_BY_ID,
    GET_PRODUCT_COLLECTIONS,
    GET_PRODUCT_FULL_BY_HANDLE,
    GET_PRODUCT_FULL_BY_ID,
    GET_PRODUCT_SEO_BY_ID,
    GET_PRODUCT_VARIANTS_POLICY,
    GET_PRODUCTS,
    GET_PRODUCTS_BY_COLLECTION,
    GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS,
    GET_PRODUCTS_WITH_DESCRIPTIONS,
    UPDATE_PRODUCT,
    UPDATE_PRODUCT_STATUS,
    UPDATE_PRODUCT_TAGS,
    UPDATE_PRODUCT_VARIANTS_POLICY,
)

# Page size for paginated variant reads in read_product / read_product_full.
# Kept separate from VARIANTS_PAGE_CAP (policy path) so the two can diverge.
_VARIANTS_PAGE_CAP = 50

# Shopify's per-request ceiling for the variants connection is 250. The policy
# read is fully paginated via client.paginate() with page_size=VARIANTS_PAGE_CAP.
VARIANTS_PAGE_CAP = 250

# Story 10.72 — the outer products-connection walk in read_products.
#
# Story 10.76 widened the consumer list: read_products_by_collection,
# read_products_with_descriptions and read_collection_with_descriptions now
# derive their budget from these same two constants, across three further query
# shapes. Of those, only read_products_by_collection can actually reach all 10
# pages — the other two are held to a single request by their tool's 250 clamp.
#
# 250 is Shopify's per-connection maximum, so it costs the fewest round trips
# for a given result set, and it keeps a store of under 250 products resolving
# in exactly one request, as it did before this story.
#
# Measured against the live store on 2026-09-04 rather than assumed: the
# GET_PRODUCTS shape at first=250 reports requestedQueryCost=112
# (actualQueryCost=9) against a 2000-point bucket restoring at 100/s. The
# 10-page worst case is ~1120 requested points spread over 10 requests — inside
# the budget even on a 1000-point standard-plan bucket, and far below the
# 1000-point per-query maximum that would reject the request outright.
#
# That figure is GET_PRODUCTS-SPECIFIC and was never re-measured for the three
# shapes Story 10.76 added. GET_PRODUCTS_BY_COLLECTION nests the same four
# scalar fields one level deeper and is the one that can issue all 10 pages, so
# it is the one worth probing first. Re-measure if the nested
# variants(first: 50) selection ever grows, or before raising either constant.
PRODUCTS_PAGE_SIZE = 250

# Page budget for those walks: 10 x 250 = 2500 products before a read reports
# capped. Kept explicit rather than leaning on client.paginate()'s default,
# because the tools' truncation warning describes this budget.
PRODUCTS_MAX_PAGES = 10

# Fixed Shopify search-syntax fragments for the status filter, keyed by the
# validated status constant. read_products looks a status up in this table
# instead of interpolating the caller's string into the query, so a value that
# is not a key here cannot reach Shopify at all. Keys mirror
# tools.products.PRODUCT_STATUS_VALUES; the mapping lives here rather than
# being imported from the tools layer, which the operations layer must not
# depend on (Story 10.23 / A5).
#
# Story 10.74 added UNLISTED. Schema introspection of ProductStatus on
# 2026-09-05 returned all four values, none deprecated, on API versions
# 2024-01, 2025-10 and 2026-01 — the last being the version this project
# targets (settings.shopify_api_version defaults to "2026-01"). This table was
# written incomplete from the start, while the store held a product in that
# state the whole time.
PRODUCT_STATUS_QUERY = {
    "ACTIVE": "status:ACTIVE",
    "DRAFT": "status:DRAFT",
    "ARCHIVED": "status:ARCHIVED",
    "UNLISTED": "status:UNLISTED",
}


# ---------- reads ----------


def _require_discriminator(product_id: str, handle: str) -> None:
    """Fail loud when the identifier pair is unusable — neither, or both.

    Neither: the MCP tool layer guards this upstream, but as standalone
    operations (callable from CLI/scripts) the by-id/by-handle reads would
    otherwise silently issue a ``productByHandle(handle: "")`` query.

    Both: Story 10.68 (``T-10.65-refuse-both-fanout``). These three reads used
    to take ``product_id`` and discard ``handle``, so a caller naming two
    different products got the first with no signal the second was ignored.
    The rule now comes from ``shopify._identifiers``, shared with
    ``operations/publications.py`` and with ``tools/_product_resolver.py`` —
    see that module for why one definition rather than three parallel copies.
    """
    reject_both_identifiers(product_id, handle)
    if not product_id and not handle:
        raise ValueError("provide either product_id or handle")


def _limit_budget(limit: int) -> tuple[int, int]:
    """Translate a caller ``limit`` into ``(page_size, max_pages)`` for paginate().

    Shared by every product read that walks an outer connection (Story 10.76 —
    ``read_products`` and the two description reads), so the guard below exists
    once rather than in three copies that can drift apart.

    ``limit`` can only *narrow* the request budget, never widen it: a small
    limit costs one small request instead of a full walk, and the ``min()`` on
    ``max_pages`` is what stops an over-large model-facing value authorising
    thousands of sequential requests — the unbounded worst case that was Story
    10.72's High review finding. Zero (or negative) means no caller cap.
    """
    if limit <= 0:
        return PRODUCTS_PAGE_SIZE, PRODUCTS_MAX_PAGES
    page_size = min(limit, PRODUCTS_PAGE_SIZE)
    return page_size, min((limit + page_size - 1) // page_size, PRODUCTS_MAX_PAGES)


def _collection_head(first_page: dict[str, Any]) -> dict[str, Any] | None:
    """The collection's own fields from paginate()'s first page, WITHOUT its
    products connection.

    That connection holds page ONE only. Returning it beside the complete node
    list would leave the pre-10.76 call-site idiom
    ``col.get("products", {}).get("nodes", [])`` compiling and silently yielding
    a truncated list — the exact silent truncation this story exists to remove.
    Stripped rather than merely documented, so the trap cannot be re-entered by
    the next caller. Builds a new dict rather than popping, so the caller's
    response object is not mutated.
    """
    col = first_page.get("collectionByHandle")
    if col is None:
        return None
    return {k: v for k, v in col.items() if k != "products"}


def _apply_limit(
    nodes: list[dict[str, Any]], capped: bool, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """Trim a walk's nodes to ``limit`` and fold the overshoot into ``capped``.

    A limit that is not a whole number of pages overshoots; the discarded
    remainder is itself proof that more products exist, so it sets ``capped``
    even when the walk itself finished cleanly.
    """
    if limit > 0:
        return nodes[:limit], capped or len(nodes) > limit
    return nodes, capped


def read_products(
    client: GraphQLClient, *, status: str = "", limit: int = 0
) -> tuple[list[dict[str, Any]], bool]:
    """List products (id, title, handle, status, variants), paginating the
    outer products connection.

    Returns ``(product_nodes, capped)`` — the same tuple convention
    ``read_product`` uses — where ``capped`` is True when the walk stopped with
    more products still available. Story 10.72 replaced the single
    ``first: 250`` request this used to issue, which truncated silently.

    ``status`` narrows the connection to one of ``PRODUCT_STATUS_QUERY``'s keys.
    It is looked up in that table, never interpolated; an unmapped value raises
    ``ValueError`` before any request is issued. Empty means no filter.

    ``limit`` caps how many products are returned. It can only *narrow* the
    request budget, never widen it: a small limit costs one small request
    instead of a full walk, and a limit larger than
    ``PRODUCTS_PAGE_SIZE * PRODUCTS_MAX_PAGES`` still stops at that ceiling and
    reports ``capped``. Zero (or negative) means no caller cap.
    """
    search: str | None = None
    if status:
        if status not in PRODUCT_STATUS_QUERY:
            raise ValueError(
                "unsupported product status filter; expected one of "
                + ", ".join(PRODUCT_STATUS_QUERY)
            )
        search = PRODUCT_STATUS_QUERY[status]

    page_size, max_pages = _limit_budget(limit)
    _, nodes, capped = client.paginate(
        GET_PRODUCTS,
        {"query": search},
        connection_path=["products"],
        page_size=page_size,
        max_pages=max_pages,
    )
    return _apply_limit(nodes, capped, limit)


def read_product(
    client: GraphQLClient, *, product_id: str = "", handle: str = ""
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a single product (core fields) by id or handle, paginating variants.

    Returns ``(product_or_None, variant_nodes, capped)``. The caller passes
    exactly one discriminator: supplying both raises ``ValueError`` before any
    network call (Story 10.68 — this **replaces** the ``product_id``-wins
    precedence this function documented through Story 10.67).
    """
    _require_discriminator(product_id, handle)
    if product_id:
        data, variants, capped = client.paginate(
            GET_PRODUCT_BY_ID,
            {"id": to_gid("Product", product_id)},
            connection_path=["product", "variants"],
            page_size=_VARIANTS_PAGE_CAP,
        )
        return data.get("product"), variants, capped
    data, variants, capped = client.paginate(
        GET_PRODUCT_BY_HANDLE,
        {"handle": handle},
        connection_path=["productByHandle", "variants"],
        page_size=_VARIANTS_PAGE_CAP,
    )
    return data.get("productByHandle"), variants, capped


def read_product_full(
    client: GraphQLClient, *, product_id: str = "", handle: str = ""
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a full product record by id or handle, paginating variants.

    Returns ``(product_or_None, variant_nodes, capped)``.
    """
    _require_discriminator(product_id, handle)
    if product_id:
        data, variants, capped = client.paginate(
            GET_PRODUCT_FULL_BY_ID,
            {"id": to_gid("Product", product_id)},
            connection_path=["product", "variants"],
            page_size=_VARIANTS_PAGE_CAP,
        )
        return data.get("product"), variants, capped
    data, variants, capped = client.paginate(
        GET_PRODUCT_FULL_BY_HANDLE,
        {"handle": handle},
        connection_path=["productByHandle", "variants"],
        page_size=_VARIANTS_PAGE_CAP,
    )
    return data.get("productByHandle"), variants, capped


def read_product_description(
    client: GraphQLClient, *, product_id: str = "", handle: str = ""
) -> dict[str, Any] | None:
    """Read a single product's core record (for its body_html) by id or handle."""
    _require_discriminator(product_id, handle)
    if product_id:
        data = client.execute(GET_PRODUCT_BY_ID, {"id": to_gid("Product", product_id)})
        return data.get("product")
    data = client.execute(GET_PRODUCT_BY_HANDLE, {"handle": handle})
    return data.get("productByHandle")


def read_product_seo(client: GraphQLClient, product_id: str) -> dict[str, Any] | None:
    """Read a product's id/title/seo fields."""
    data = client.execute(GET_PRODUCT_SEO_BY_ID, {"id": to_gid("Product", product_id)})
    return data.get("product")


def read_product_collections(client: GraphQLClient, product_id: str) -> dict[str, Any] | None:
    """Read a product with its collection memberships (capped at 250)."""
    data = client.execute(GET_PRODUCT_COLLECTIONS, {"id": to_gid("Product", product_id)})
    return data.get("product")


def read_products_by_collection(
    client: GraphQLClient, collection_handle: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a collection (by handle) with its products, paginating the nested
    products connection.

    Returns ``(collection_or_None, product_nodes, capped)`` — the tuple
    convention ``read_product`` and ``read_products`` already use — where
    ``capped`` is True when the walk stopped with more products still
    available. Story 10.76 replaced the single ``first: 250`` request this used
    to issue, which truncated silently.

    The collection's own ``id``/``title``/``handle`` are taken from the FIRST
    page, which is what ``paginate()``'s first return value exists for. A
    missing handle yields ``None`` for the collection, so "no such collection"
    stays distinguishable from "collection with no products". The returned dict
    carries no ``products`` key — see ``_collection_head``.
    """
    first_page, nodes, capped = client.paginate(
        GET_PRODUCTS_BY_COLLECTION,
        {"handle": collection_handle},
        connection_path=["collectionByHandle", "products"],
        page_size=PRODUCTS_PAGE_SIZE,
        max_pages=PRODUCTS_MAX_PAGES,
    )
    return _collection_head(first_page), nodes, capped


def read_products_with_descriptions(
    client: GraphQLClient, *, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """Read products (with body_html) across the store, paginating the outer
    products connection.

    Returns ``(product_nodes, capped)``. ``limit`` is a **total across pages**,
    not a page size (Story 10.76) — under the old single-shot request the two
    were the same number, so nothing observable changed for a caller.
    """
    page_size, max_pages = _limit_budget(limit)
    _, nodes, capped = client.paginate(
        GET_PRODUCTS_WITH_DESCRIPTIONS,
        {},
        connection_path=["products"],
        page_size=page_size,
        max_pages=max_pages,
    )
    return _apply_limit(nodes, capped, limit)


def read_collection_with_descriptions(
    client: GraphQLClient, collection_handle: str, limit: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a collection (by handle) with its products' body_html, paginating
    the nested products connection.

    Returns ``(collection_or_None, product_nodes, capped)``, with the same
    first-page and missing-handle semantics as ``read_products_by_collection``.
    ``limit`` is a total across pages (Story 10.76).
    """
    page_size, max_pages = _limit_budget(limit)
    first_page, nodes, capped = client.paginate(
        GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS,
        {"handle": collection_handle},
        connection_path=["collectionByHandle", "products"],
        page_size=page_size,
        max_pages=max_pages,
    )
    nodes, capped = _apply_limit(nodes, capped, limit)
    return _collection_head(first_page), nodes, capped


def read_product_variants_policy(
    client: GraphQLClient, product_id: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a product and all its variants' inventoryPolicy, paginated.

    Returns ``(product_or_None, variant_nodes, capped)``.
    """
    data, variants, capped = client.paginate(
        GET_PRODUCT_VARIANTS_POLICY,
        {"id": to_gid("Product", product_id)},
        connection_path=["product", "variants"],
        page_size=VARIANTS_PAGE_CAP,
    )
    return data.get("product"), variants, capped


def fetch_product_core(client: GraphQLClient, product_id: str) -> dict[str, Any] | None:
    """Single (non-paginated) read of a product's core fields by id — used by
    the title/description/status write previews."""
    data = client.execute(GET_PRODUCT_BY_ID, {"id": to_gid("Product", product_id)})
    return data.get("product")


def fetch_product_full_record(client: GraphQLClient, product_id: str) -> dict[str, Any] | None:
    """Single (non-paginated) read of a product's full record by id — used by
    the tags write preview (which needs the current tag list)."""
    data = client.execute(GET_PRODUCT_FULL_BY_ID, {"id": to_gid("Product", product_id)})
    return data.get("product")


# ---------- writes (return the raw mutation result) ----------


def update_product_title(
    client: GraphQLClient, product_id: str, new_title: str, target_handle: str
) -> dict[str, Any]:
    """Execute a productUpdate setting title (and explicit handle)."""
    return client.execute(
        UPDATE_PRODUCT,
        {
            "input": {
                "id": to_gid("Product", product_id),
                "title": new_title,
                "handle": target_handle,
            }
        },
    )


def update_product_description(
    client: GraphQLClient, product_id: str, new_description: str
) -> dict[str, Any]:
    """Execute a productUpdate setting descriptionHtml."""
    return client.execute(
        UPDATE_PRODUCT,
        {"input": {"id": to_gid("Product", product_id), "descriptionHtml": new_description}},
    )


def update_product_seo(
    client: GraphQLClient, product_id: str, seo_input: dict[str, str]
) -> dict[str, Any]:
    """Execute a productUpdate setting the seo sub-input."""
    return client.execute(
        UPDATE_PRODUCT,
        {"input": {"id": to_gid("Product", product_id), "seo": seo_input}},
    )


def update_product_tags(client: GraphQLClient, product_id: str, tags: list[str]) -> dict[str, Any]:
    """Execute a productUpdate setting the tag list verbatim."""
    return client.execute(
        UPDATE_PRODUCT_TAGS,
        {"input": {"id": to_gid("Product", product_id), "tags": tags}},
    )


def update_product_status(
    client: GraphQLClient, product_id: str, new_status: str
) -> dict[str, Any]:
    """Execute a productUpdate setting status."""
    return client.execute(
        UPDATE_PRODUCT_STATUS,
        {"input": {"id": to_gid("Product", product_id), "status": new_status}},
    )


def update_variant_inventory_policy(
    client: GraphQLClient, product_id: str, variants_input: list[dict[str, str]]
) -> dict[str, Any]:
    """Execute a productVariantsBulkUpdate setting inventoryPolicy per variant."""
    return client.execute(
        UPDATE_PRODUCT_VARIANTS_POLICY,
        {"productId": to_gid("Product", product_id), "variants": variants_input},
    )
