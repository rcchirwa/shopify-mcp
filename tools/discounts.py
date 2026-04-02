"""
Discount tools — read and create discount codes.

create_discount_code requires confirm=True.
"""

from mcp.server import Server
from shopify_client import ShopifyClient
from tools._log import log_write


def register(server: Server, client: ShopifyClient):

    @server.tool()
    def get_discount_codes() -> str:
        """List discount codes (price rules) for the store."""
        data = client.get("/price_rules.json", {"limit": 50})
        rules = data.get("price_rules", [])
        if not rules:
            return "No discount codes found."

        lines = [f"Discount codes ({len(rules)} price rules found):\n"]
        for rule in rules:
            discount_type = rule.get("value_type", "")
            value = rule.get("value", "")
            lines.append(
                f"  [{rule['id']}] {rule['title']}\n"
                f"    Type: {discount_type} | Value: {value} | "
                f"Usage limit: {rule.get('usage_limit', 'unlimited')} | "
                f"Ends: {rule.get('ends_at', 'no expiry')}"
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

        price_rule_payload = {
            "price_rule": {
                "title": title,
                "target_type": "line_item",
                "target_selection": "all",
                "allocation_method": "across",
                "value_type": "percentage",
                "value": str(value),
                "customer_selection": "all",
                "starts_at": "2024-01-01T00:00:00Z",
            }
        }
        if usage_limit > 0:
            price_rule_payload["price_rule"]["usage_limit"] = usage_limit

        rule_data = client.post("/price_rules.json", price_rule_payload)
        rule_id = rule_data["price_rule"]["id"]

        client.post(
            f"/price_rules/{rule_id}/discount_codes.json",
            {"discount_code": {"code": code}},
        )
        log_write(
            "create_discount_code",
            f"title={title} code={code} value={value}% usage_limit={usage_limit}",
        )
        return f"Done. Price rule id={rule_id} created.\n{preview}"
