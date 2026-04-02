"""
Order tools — read-only access to Shopify orders.
"""

from mcp.server import Server
from shopify_client import ShopifyClient


def register(server: Server, client: ShopifyClient):

    @server.tool()
    def get_orders(limit: int = 20) -> str:
        """
        List recent orders with id, total price, line items, and traffic source.
        limit: number of orders to return (max 250).
        """
        limit = min(limit, 250)
        data = client.get(
            "/orders.json",
            {
                "limit": limit,
                "status": "any",
                "fields": "id,name,total_price,line_items,referring_site,landing_site,created_at",
            },
        )
        orders = data.get("orders", [])
        if not orders:
            return "No orders found."

        lines = [f"Recent orders ({len(orders)}):\n"]
        for o in orders:
            items = ", ".join(
                f"{li['name']} x{li['quantity']}" for li in o.get("line_items", [])
            )
            traffic = o.get("referring_site") or o.get("landing_site") or "direct / unknown"
            lines.append(
                f"  [{o['id']}] {o['name']} — ${o['total_price']} — {o['created_at'][:10]}\n"
                f"    Items: {items}\n"
                f"    Source: {traffic}"
            )
        return "\n".join(lines)

    @server.tool()
    def get_order(order_id: str) -> str:
        """Get a single order by id."""
        data = client.get(f"/orders/{order_id}.json")
        o = data.get("order", {})
        if not o:
            return f"Order {order_id} not found."

        items = "\n".join(
            f"  • {li['name']} x{li['quantity']} — ${li['price']}"
            for li in o.get("line_items", [])
        )
        return (
            f"Order: {o['name']} (id: {o['id']})\n"
            f"Date: {o['created_at']}\n"
            f"Total: ${o['total_price']}\n"
            f"Status: {o.get('financial_status')} / {o.get('fulfillment_status')}\n"
            f"Traffic source: {o.get('referring_site') or 'direct'}\n"
            f"Line items:\n{items}"
        )
