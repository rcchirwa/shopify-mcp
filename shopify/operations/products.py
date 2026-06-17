"""Typed product operations — business logic over ``shopify.queries.products``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs GID coercion + query/mutation execution, returning structured data.
No MCP imports and no output formatting, so these are callable from non-MCP
entry points (CLI, scripts, tests). ``tools/products.py`` layers param coercion,
the preview/confirm flow, and string formatting on top.
"""

from typing import Any

from shopify._client import GraphQLClient
from shopify._ids import to_gid
from shopify.queries.products import (
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


# ---------- reads ----------


def read_products(client: GraphQLClient) -> list[dict[str, Any]]:
    """List products with id, title, handle, status, and variants."""
    data = client.execute(GET_PRODUCTS, {"first": 250})
    return data.get("products", {}).get("nodes", [])


def read_product(
    client: GraphQLClient, *, product_id: str = "", handle: str = ""
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a single product (core fields) by id or handle, paginating variants.

    Returns ``(product_or_None, variant_nodes, capped)``. The caller decides
    which discriminator to pass; ``product_id`` wins when both are given.
    """
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
) -> dict[str, Any] | None:
    """Read a collection (by handle) with its products. None if not found."""
    data = client.execute(GET_PRODUCTS_BY_COLLECTION, {"handle": collection_handle, "first": 250})
    return data.get("collectionByHandle")


def read_products_with_descriptions(client: GraphQLClient, *, limit: int) -> list[dict[str, Any]]:
    """Read products (with body_html) across the store, up to ``limit``."""
    data = client.execute(GET_PRODUCTS_WITH_DESCRIPTIONS, {"first": limit})
    return data.get("products", {}).get("nodes", [])


def read_collection_with_descriptions(
    client: GraphQLClient, collection_handle: str, limit: int
) -> dict[str, Any] | None:
    """Read a collection (by handle) with its products' body_html. None if not found."""
    data = client.execute(
        GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS,
        {"handle": collection_handle, "first": limit},
    )
    return data.get("collectionByHandle")


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
