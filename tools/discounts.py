"""
Discount tools — read and create discount codes.

create_discount_code requires confirm=True.
"""

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient, to_gid, from_gid
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
            return preview + "\n\nTo apply, call again with confirm=True."

        price_rule_input = {
            "title": title,
            "target": "LINE_ITEM",
            "allocationMethod": "ACROSS",
            "valueType": "PERCENTAGE",
            "value": str(value),
            "customerSelection": {"forAllCustomers": True},
            "startsAt": "2024-01-01T00:00:00Z",
        }
        if usage_limit > 0:
            price_rule_input["usageLimit"] = usage_limit

        rule_result = client.execute(CREATE_PRICE_RULE, {"input": price_rule_input})
        pr_errors = rule_result.get("priceRuleCreate", {}).get("priceRuleUserErrors", [])
        if pr_errors:
            msgs = "; ".join(f"{e['field']}: {e['message']}" for e in pr_errors)
            return f"Error creating price rule: {msgs}"

        rule_id = rule_result.get("priceRuleCreate", {}).get("priceRule", {}).get("id")

        code_result = client.execute(CREATE_DISCOUNT_CODE, {
            "priceRuleId": rule_id,
            "code": code,
        })
        code_errors = code_result.get("priceRuleDiscountCodeCreate", {}).get("userErrors", [])
        if code_errors:
            msgs = "; ".join(f"{e['field']}: {e['message']}" for e in code_errors)
            return f"Error attaching discount code: {msgs}"

        log_write(
            "create_discount_code",
            f"title={title} code={code} value={value}% usage_limit={usage_limit}",
        )
        return f"Done. Price rule id={from_gid(rule_id)} created.\n{preview}"
