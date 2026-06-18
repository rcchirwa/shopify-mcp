"""
Discount tools — read and create discount codes.

Thin MCP-tool surface over ``shopify.operations.discounts``: this module keeps
param coercion, the PriceRuleInput assembly, the preview/confirm flow, and output
formatting; the GraphQL strings live in ``shopify.queries.discounts`` and the
data access in ``shopify.operations.discounts`` (Story 10.27 / A5).

create_discount_code requires confirm=True.
"""

from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify.operations import discounts as ops
from shopify.queries.discounts import (
    CREATE_DISCOUNT_CODE,
    CREATE_PRICE_RULE,
    GET_PRICE_RULES,
)
from shopify_client import ShopifyClient
from tools._gid import from_gid
from tools._log import log_write
from tools._response import format_user_errors, with_confirm_hint

# The GraphQL strings now live in shopify.queries.discounts. They are re-exported
# here so existing callers/tests (`from tools.discounts import GET_PRICE_RULES`)
# keep resolving to the same objects the operations layer executes.
__all__ = [
    "CREATE_DISCOUNT_CODE",
    "CREATE_PRICE_RULE",
    "GET_PRICE_RULES",
    "register",
]


def register(server: FastMCP, client: ShopifyClient) -> None:

    @server.tool()
    def get_discount_codes() -> str:
        """List discount codes (price rules) for the store."""
        rules = ops.read_price_rules(client)
        if not rules:
            return "No discount codes found."

        lines = [f"Discount codes ({len(rules)} price rules found):\n"]
        for rule in rules:
            discount_type = rule.get("valueType", "")
            value = rule.get("value", "")
            lines.append(
                f"  [{from_gid(rule['id'])}] {rule['title']}\n"
                f"    Type: {discount_type} | Value: {value} | "
                f"Usage limit: {rule.get('usageLimit') or 'unlimited'} | "
                f"Ends: {rule.get('endsAt') or 'no expiry'}"
            )
        return "\n".join(lines)

    @server.tool()
    def create_discount_code(
        title: str,
        code: str,
        percentage_off: float,
        usage_limit: int = 0,
        confirm: bool = False,
    ) -> str:
        """
        Create a new percentage-off discount code.
        percentage_off: e.g. 20 = 20% off.
        usage_limit: 0 = unlimited.
        Returns a preview unless confirm=True.
        """
        value = -abs(percentage_off)  # Shopify expects negative value for discounts

        preview = (
            f"PREVIEW — New discount code\n"
            f"  Title         : {title}\n"
            f"  Code          : {code}\n"
            f"  Discount      : {percentage_off}% off\n"
            f"  Usage limit   : {'unlimited' if usage_limit == 0 else usage_limit}"
        )

        if not confirm:
            return with_confirm_hint(preview)

        price_rule_input: dict[str, Any] = {
            "title": title,
            "target": "LINE_ITEM",
            "allocationMethod": "ACROSS",
            "valueType": "PERCENTAGE",
            "value": str(value),
            "customerSelection": {"forAllCustomers": True},
            "startsAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if usage_limit > 0:
            price_rule_input["usageLimit"] = usage_limit

        rule_result = ops.create_price_rule(client, price_rule_input)
        err = format_user_errors(
            rule_result,
            "priceRuleCreate",
            error_key="priceRuleUserErrors",
            prefix="Error creating price rule",
        )
        if err:
            return err

        # priceRule is None when the mutation shape-drifts or userErrors are
        # empty but the server-side commit still failed — guard with `or {}`
        # (same pattern as tools/inventory.py `.get("inventoryItem") or {}`).
        rule_id = ((rule_result.get("priceRuleCreate") or {}).get("priceRule") or {}).get("id")
        if not rule_id:
            return "Error: price rule created but no ID returned."

        code_result = ops.create_discount_code(client, rule_id, code)
        err = format_user_errors(
            code_result,
            "priceRuleDiscountCodeCreate",
            prefix="Error attaching discount code",
        )
        if err:
            return err

        log_write(
            "create_discount_code",
            f"title={title} code={code} value={value}% usage_limit={usage_limit}",
        )
        return f"Done. Price rule id={from_gid(rule_id)} created.\n{preview}"
