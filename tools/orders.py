"""
Order tools — read-only access to Shopify orders.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_client import ShopifyClient, from_gid

GET_ORDERS = """
query GetOrders($first: Int!) {
  orders(first: $first) {
    nodes {
      id
      name
      createdAt
      totalPriceSet { shopMoney { amount } }
      lineItems(first: 50) {
        nodes {
          name
          quantity
        }
      }
      referringSite
      landingSite
    }
  }
}
"""

GET_ORDER_BY_ID = """
query GetOrderById($id: ID!) {
  order(id: $id) {
    id
    name
    createdAt
    totalPriceSet { shopMoney { amount } }
    displayFinancialStatus
    displayFulfillmentStatus
    referringSite
    lineItems(first: 50) {
      nodes {
        name
        quantity
        originalUnitPriceSet { shopMoney { amount } }
      }
    }
  }
}
"""


def register(server: FastMCP, client: ShopifyClient) -> None:

    @server.tool()
    def get_orders(limit: int = 20) -> str:
        """
        List recent orders with id, total price, line items, and traffic source.
        limit: number of orders to return (max 250).
        """
        limit = min(limit, 250)
        data = client.execute(GET_ORDERS, {"first": limit})
        orders = data.get("orders", {}).get("nodes", [])
        if not orders:
            return "No orders found."

        lines = [f"Recent orders ({len(orders)}):\n"]
        for o in orders:
            items = ", ".join(
                f"{li['name']} x{li['quantity']}" for li in o.get("lineItems", {}).get("nodes", [])
            )
            traffic = o.get("referringSite") or o.get("landingSite") or "direct / unknown"
            # `or {}` guards against nulls at any level — Shopify can return
            # totalPriceSet=null on orders still in a pending/edited state.
            total = ((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount", "N/A")
            lines.append(
                f"  [{from_gid(o['id'])}] {o['name']} — ${total} — {o['createdAt'][:10]}\n"
                f"    Items: {items}\n"
                f"    Source: {traffic}"
            )
        return "\n".join(lines)

    @server.tool()
    def get_order(order_id: str) -> str:
        """Get a single order by id."""
        from shopify_client import to_gid

        data = client.execute(GET_ORDER_BY_ID, {"id": to_gid("Order", order_id)})
        o = data.get("order")
        if not o:
            return f"Order {order_id} not found."

        total = ((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount", "N/A")

        def _unit_price(li: dict[str, Any]) -> str:
            return ((li.get("originalUnitPriceSet") or {}).get("shopMoney") or {}).get(
                "amount", "N/A"
            )

        items = "\n".join(
            f"  • {li['name']} x{li['quantity']} — ${_unit_price(li)}"
            for li in (o.get("lineItems") or {}).get("nodes", []) or []
        )
        return (
            f"Order: {o['name']} (id: {from_gid(o['id'])})\n"
            f"Date: {o['createdAt']}\n"
            f"Total: ${total}\n"
            f"Status: {o.get('displayFinancialStatus')} / {o.get('displayFulfillmentStatus')}\n"
            f"Traffic source: {o.get('referringSite') or 'direct'}\n"
            f"Line items:\n{items}"
        )
