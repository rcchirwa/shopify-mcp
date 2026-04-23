"""
Discount tools — read and create discount codes.

create_discount_code requires confirm=True.
"""

from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP
from shopify_client import (
    ShopifyClient,
    format_user_errors,
    to_gid,
    from_gid,
    with_confirm_hint,
)
from tools._log import log_write

GET_PRICE_RULES = """
query GetPriceRules($first: Int!) {
  priceRules(first: $first) {
    nodes {
      id
      title
      valueType
      value
      usageLimit
      endsAt
    }
  }
}
"""

CREATE_PRICE_RULE = """
mutation CreatePriceRule($input: PriceRuleInput!) {
  priceRuleCreate(priceRule: $input) {
    priceRule { id }
    priceRuleUserErrors { field message }
  }
}
"""

CREATE_DISCOUNT_CODE = """
mutation CreateDiscountCode($priceRuleId: ID!, $code: String!) {
  priceRuleDiscountCodeCreate(priceRuleId: $priceRuleId, code: $code) {
    priceRuleDiscountCode { code }
    userErrors { field message }
  }
}
"""


def register(server: FastMCP, client: ShopifyClient):

    @server.tool()
    def get_discount_codes() -> str:
        """List discount codes (price rules) for the store."""
        data = client.execute(GET_PRICE_RULES, {"first": 50})
        rules = data.get("priceRules", {}).get("nodes", [])
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

        price_rule_input = {
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

        rule_result = client.execute(CREATE_PRICE_RULE, {"input": price_rule_input})
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
        rule_id = (
            (rule_result.get("priceRuleCreate") or {})
            .get("priceRule") or {}
        ).get("id")
        if not rule_id:
            return "Error: price rule created but no ID returned."

        code_result = client.execute(CREATE_DISCOUNT_CODE, {
            "priceRuleId": rule_id,
            "code": code,
        })
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
