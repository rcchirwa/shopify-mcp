"""
Discount tools — read and create discount codes.

Thin MCP-tool surface over ``shopify.operations.discounts``: this module keeps
param coercion, the PriceRuleInput assembly, the preview/confirm flow, and output
formatting; the GraphQL strings live in ``shopify.queries.discounts`` and the
data access in ``shopify.operations.discounts`` (Story 10.27 / A5).

create_discount_code requires confirm=True.
"""

from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_mcp.client import ShopifyClient
from shopify_mcp.shopify.operations import discounts as ops
from shopify_mcp.shopify.queries.discounts import (
    CREATE_DISCOUNT_CODE,
    CREATE_PRICE_RULE,
    GET_CODE_DISCOUNTS,
)
from shopify_mcp.tools._gid import from_gid
from shopify_mcp.tools._log import log_write
from shopify_mcp.tools._response import format_user_errors, with_confirm_hint

# Shopify rejects a 0% or negative discount, and a >100% value would zero out
# (or overpay) a line item — bound client-side rather than let a nonsensical
# code preview as legitimate (SEC-07).
DISCOUNT_PCT_MIN = 0
DISCOUNT_PCT_MAX = 100

# The GraphQL strings now live in shopify.queries.discounts. They are re-exported
# here so existing callers/tests (`from tools.discounts import GET_CODE_DISCOUNTS`)
# keep resolving to the same objects the operations layer executes.
__all__ = [
    "CREATE_DISCOUNT_CODE",
    "CREATE_PRICE_RULE",
    "GET_CODE_DISCOUNTS",
    "register",
]


def register(server: FastMCP, client: ShopifyClient) -> None:

    @server.tool()
    def get_discount_codes() -> str:
        """List discount codes for the store."""
        nodes = ops.read_code_discounts(client)
        if not nodes:
            return "No discount codes found."

        lines = [f"Discount codes ({len(nodes)} found):\n"]
        for node in nodes:
            discount = node.get("discount") or {}
            codes_conn = discount.get("codes") or {}
            # `or []`, not `.get("nodes", [])`: a permissions-trimmed / shape-
            # drifted response can return "nodes": null, which the dict-default
            # form would not catch.
            codes = [c["code"] for c in (codes_conn.get("nodes") or []) if c.get("code")]
            codes_str = ", ".join(codes) if codes else "(no code)"
            if (codes_conn.get("pageInfo") or {}).get("hasNextPage"):
                codes_str += " (+more not shown)"
            value = (discount.get("customerGets") or {}).get("value") or {}
            value_type = value.get("__typename")
            if value_type == "DiscountPercentage":
                value_line = f"{value['percentage'] * 100:g}% off"
            elif value_type == "DiscountAmount":
                amount = ((value.get("amount") or {}).get("amount")) or "N/A"
                value_line = f"${amount} off"
            else:
                value_line = discount.get("__typename", "")
            lines.append(
                f"  [{from_gid(node['id'])}] {discount.get('title', '')}\n"
                f"    Codes: {codes_str} | {value_line} | Status: {discount.get('status', '')} | "
                f"Usage limit: {discount.get('usageLimit') or 'unlimited'} | "
                f"Ends: {discount.get('endsAt') or 'no expiry'}"
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
        percentage_off: e.g. 20 = 20% off. Must be > 0 and <= 100.
        usage_limit: 0 = unlimited.
        Returns a preview unless confirm=True.
        """
        if not (DISCOUNT_PCT_MIN < percentage_off <= DISCOUNT_PCT_MAX):
            return (
                f"Error: percentage_off must be > {DISCOUNT_PCT_MIN} and "
                f"<= {DISCOUNT_PCT_MAX} (got {percentage_off})."
            )

        value = -percentage_off  # Shopify expects negative value for discounts

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
            "startsAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
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

        code_result = ops.create_price_rule_discount_code(client, rule_id, code)
        err = format_user_errors(
            code_result,
            "priceRuleDiscountCodeCreate",
            prefix="Error attaching discount code",
        )
        if err:
            return err

        # SEC-12: the discount code is masked in the durable audit log. The
        # price-rule id below already identifies the discount, and the code is
        # recoverable from Shopify — so plaintext buys no audit value while
        # leaving a secret-shaped string in a local file.
        log_write(
            "create_discount_code",
            f"title={title} code=*** value={value}% usage_limit={usage_limit}",
        )
        return f"Done. Price rule id={from_gid(rule_id)} created.\n{preview}"
