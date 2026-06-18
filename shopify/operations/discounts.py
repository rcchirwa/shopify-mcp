"""Typed discounts operations — data access over ``shopify.queries.discounts``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs the GraphQL-variable building + query/mutation execution, returning
the raw Shopify response (writes) or the extracted node list (the price-rules
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

from shopify._client import GraphQLClient
from shopify.queries.discounts import (
    CREATE_DISCOUNT_CODE,
    CREATE_PRICE_RULE,
    GET_PRICE_RULES,
)

# Fixed slice for the price-rules listing read (get_discount_codes). Matches the
# inline ``{"first": 50}`` the tool issued before the migration.
PRICE_RULES_PAGE_SIZE = 50


# ---------- reads ----------


def read_price_rules(client: GraphQLClient) -> list[dict[str, Any]]:
    """List price rules (discount codes) for the store — returns the node list."""
    data = client.execute(GET_PRICE_RULES, {"first": PRICE_RULES_PAGE_SIZE})
    return data.get("priceRules", {}).get("nodes", [])


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
