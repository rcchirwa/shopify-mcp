"""Typed discounts operations — data access over ``shopify.queries.discounts``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs the GraphQL-variable building + query/mutation execution, returning
the raw Shopify response (writes) or the extracted node list (the code-discounts
read). No MCP imports and no output formatting, so these are callable from
non-MCP entry points (CLI, scripts, tests) — Story 10.27 / A5, AC4.
``tools/discounts.py`` layers param coercion, the PriceRuleInput assembly, the
preview/confirm flow, and string formatting on top.

GID coercion: discounts has no by-id/by-handle resolution — the price-rule GID
the second mutation needs flows straight out of the first mutation's response —
so unlike the products/catalog_hygiene operations these wrappers do no
``to_gid`` coercion.
"""

from typing import Any

from shopify_mcp.shopify._client import GraphQLClient
from shopify_mcp.shopify.queries.discounts import (
    CREATE_DISCOUNT_CODE,
    CREATE_PRICE_RULE,
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


def create_price_rule(client: GraphQLClient, price_rule_input: dict[str, Any]) -> dict[str, Any]:
    """Execute a priceRuleCreate with the supplied (already-built) PriceRuleInput."""
    return client.execute(CREATE_PRICE_RULE, {"input": price_rule_input})


def create_price_rule_discount_code(
    client: GraphQLClient, price_rule_id: str, code: str
) -> dict[str, Any]:
    """Execute a priceRuleDiscountCodeCreate attaching ``code`` to ``price_rule_id``.

    Named for the GraphQL mutation (``priceRuleDiscountCodeCreate``) rather than
    the tool: this is only the second of the tool's two write steps, so it must
    not be confused with ``tools.discounts.create_discount_code`` (the full
    price-rule-create → code-attach flow).
    """
    return client.execute(
        CREATE_DISCOUNT_CODE,
        {"priceRuleId": price_rule_id, "code": code},
    )
