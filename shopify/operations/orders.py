"""Typed orders operations — data access over ``shopify.queries.orders``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs GID coercion + GraphQL-variable building + query execution, returning
structured data — the order node list (the list read) or the order plus its
paginated line items (the single read). No MCP imports and no output formatting,
so these are callable from non-MCP entry points (CLI, scripts, tests) —
Story 10.29 / A5, AC4. ``tools/orders.py`` layers limit clamping, the
untrusted-data wrapping, and string formatting on top. ``orders`` is read-only —
no mutation wrappers.
"""

from typing import Any

from shopify._client import GraphQLClient
from shopify._ids import to_gid
from shopify.queries.orders import GET_ORDER_BY_ID, GET_ORDERS

# Page size for line-item pagination via ``client.paginate()`` on the single-order
# read — how many line items are fetched per Shopify request.
ORDER_LINE_ITEMS_PAGE_SIZE = 50


def read_orders(client: GraphQLClient, first: int) -> list[dict[str, Any]]:
    """List recent orders, returning the order node list.

    ``first`` is the already-clamped count the tool passes through (≤ 250); the
    operation does no clamping of its own."""
    data = client.execute(GET_ORDERS, {"first": first})
    return data.get("orders", {}).get("nodes", [])


def read_order(
    client: GraphQLClient, order_id: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    """Read a single order and all its line items by id, paginated.

    Returns ``(order_or_None, line_item_nodes, capped)``. ``order_or_None`` is
    None when Shopify returns ``{"order": null}`` (deleted / wrong id); ``capped``
    is True when line-item pagination hit the max-pages cap."""
    data, line_items, capped = client.paginate(
        GET_ORDER_BY_ID,
        {"id": to_gid("Order", order_id)},
        connection_path=["order", "lineItems"],
        page_size=ORDER_LINE_ITEMS_PAGE_SIZE,
    )
    return data.get("order"), line_items, capped
