"""Typed discounts operations — data access over ``shopify.queries.discounts``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs the GraphQL-variable building + query/mutation execution, returning
the raw Shopify response (writes) or the extracted node list (the code-discounts
read). No MCP imports and no output formatting, so these are callable from
non-MCP entry points (CLI, scripts, tests) — Story 10.27 / A5, AC4.
``tools/discounts.py`` layers param coercion, the ``DiscountCodeBasicInput``
assembly, the preview/confirm flow, and string formatting on top.

GID coercion: discounts has no by-id/by-handle resolution — ``discountCodeBasicCreate``
takes no id input at all (Story 9.14) — so unlike the products/catalog_hygiene
operations these wrappers do no ``to_gid`` coercion.
"""

from typing import Any

from shopify_mcp.shopify._client import GraphQLClient
from shopify_mcp.shopify.queries.discounts import (
    CREATE_DISCOUNT_CODE_BASIC,
    GET_CODE_DISCOUNTS,
)

# Fixed slice for the code-discounts listing read (get_discount_codes). Matches
# the inline ``{"first": 50}`` the tool issued before the Story 9.12 migration
# off the removed PriceRule API.
CODE_DISCOUNTS_PAGE_SIZE = 50

# Per-discount redeem-code cap (each code discount's own ``codes`` connection —
# most have exactly one, but a bulk-code discount can carry thousands). Single
# source of truth for the ``$codesFirst`` variable, so the query and the
# tool-layer's cap-detection check (``pageInfo.hasNextPage``) agree on the same
# page size (same idea as GET_ORDERS_LINE_ITEM_CAP in shopify.operations.orders,
# though that one also interpolates its cap number into warning text — this
# notice doesn't need to, since it just says "more exist" without a count).
DISCOUNT_CODES_PER_DISCOUNT_CAP = 10

# `discountNodes`' `query` filter restricted to code discounts (excludes
# automatic discounts, which this tool has never listed). Confirmed live
# against 2026-01, 2026-09-14: returned only DiscountCode* union members, none
# of the four DiscountAutomatic* ones.
_CODE_DISCOUNTS_FILTER = "method:code"


# ---------- reads ----------


def read_code_discounts(client: GraphQLClient) -> list[dict[str, Any]]:
    """List code discounts for the store — returns the ``DiscountNode`` list."""
    data = client.execute(
        GET_CODE_DISCOUNTS,
        {
            "first": CODE_DISCOUNTS_PAGE_SIZE,
            "query": _CODE_DISCOUNTS_FILTER,
            "codesFirst": DISCOUNT_CODES_PER_DISCOUNT_CAP,
        },
    )
    return data.get("discountNodes", {}).get("nodes", [])


# ---------- writes (return the raw mutation result) ----------


def create_discount_code_basic(
    client: GraphQLClient, discount_input: dict[str, Any]
) -> dict[str, Any]:
    """Execute a discountCodeBasicCreate with the supplied (already-built)
    DiscountCodeBasicInput."""
    return client.execute(CREATE_DISCOUNT_CODE_BASIC, {"input": discount_input})
