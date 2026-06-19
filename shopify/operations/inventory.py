"""Typed inventory operations — data access over ``shopify.queries.inventory``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs GID coercion + GraphQL-variable building + query/mutation execution,
returning structured data (reads) or the raw Shopify response (writes). No MCP
imports and no output formatting, so these are callable from non-MCP entry points
(CLI, scripts, tests) — Story 10.28 / A5, AC4. ``tools/inventory.py`` layers param
coercion, the (variant, location) bucketing, the preview/confirm flow, and string
formatting on top.
"""

from typing import Any

from shopify._client import GraphQLClient
from shopify._ids import to_gid
from shopify.queries.inventory import (
    GET_INVENTORY_ITEM,
    GET_PRODUCT_INVENTORY,
    SET_INVENTORY,
    UPDATE_INVENTORY_ITEM_TRACKED,
)

# Page size for variant pagination via ``client.paginate()`` — how many variants
# are fetched per Shopify request on the product-inventory reads.
VARIANTS_PAGE_CAP = 50


# ---------- reads ----------


def read_product_inventory(
    client: GraphQLClient, product_id: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a product and all its variants' inventory levels, paginated.

    Returns ``(product_or_None, variant_nodes, capped)``. ``capped`` is True when
    variant pagination hit the max-pages cap.
    """
    data, variants, capped = client.paginate(
        GET_PRODUCT_INVENTORY,
        {"id": to_gid("Product", product_id)},
        connection_path=["product", "variants"],
        page_size=VARIANTS_PAGE_CAP,
    )
    return data.get("product"), variants, capped


def read_inventory_item_levels(
    client: GraphQLClient, inventory_item_id: str
) -> dict[str, Any] | None:
    """Read a single inventory item's levels by id. None when the item is absent
    (Shopify returns ``{"inventoryItem": null}`` for a deleted / wrong id)."""
    data = client.execute(GET_INVENTORY_ITEM, {"id": to_gid("InventoryItem", inventory_item_id)})
    return data.get("inventoryItem")


# ---------- writes (return the raw mutation result) ----------


def update_inventory_item_tracked(
    client: GraphQLClient, inventory_item_gid: str, tracked: bool
) -> dict[str, Any]:
    """Execute an inventoryItemUpdate toggling ``tracked`` on one inventory item.

    ``inventory_item_gid`` is the full GID taken straight from a variant's
    ``inventoryItem.id`` (already a GID, so no coercion here)."""
    return client.execute(
        UPDATE_INVENTORY_ITEM_TRACKED,
        {"id": inventory_item_gid, "input": {"tracked": tracked}},
    )


def set_inventory_on_hand(
    client: GraphQLClient, set_quantities: list[dict[str, Any]]
) -> dict[str, Any]:
    """Execute an inventorySetOnHandQuantities for the supplied (already-built)
    setQuantities entries, wrapping them with the fixed ``correction`` reason."""
    return client.execute(
        SET_INVENTORY,
        {"input": {"reason": "correction", "setQuantities": set_quantities}},
    )
