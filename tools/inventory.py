"""
Inventory tools — read and set inventory levels.

update_inventory requires confirm=True.
"""

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient
from tools._log import log_write


def register(server: FastMCP, client: ShopifyClient):

    @server.tool()
    def get_inventory(product_id: str) -> str:
        """Get inventory levels for a product and all its variants."""
        data = client.get(f"/products/{product_id}.json")
        product = data.get("product", {})
        if not product:
            return f"Product {product_id} not found."

        lines = [f"Inventory for: {product['title']} (id: {product_id})\n"]
        for variant in product.get("variants", []):
            inv_item_id = variant.get("inventory_item_id")
            # Fetch inventory levels for this item
            inv_data = client.get(
                "/inventory_levels.json",
                {"inventory_item_ids": inv_item_id},
            )
            levels = inv_data.get("inventory_levels", [])
            qty = levels[0]["available"] if levels else "N/A"
            lines.append(
                f"  • {variant['title']} — SKU: {variant.get('sku', 'N/A')} "
                f"— available: {qty} — variant_id: {variant['id']}"
            )
        return "\n".join(lines)

    @server.tool()
    def update_inventory(
        inventory_item_id: str,
        location_id: str,
        quantity: int,
        confirm: bool = False,
    ) -> str:
        """
        Set inventory quantity for a specific variant at a location.
        Returns a preview unless confirm=True.
        """
        # Fetch current level
        inv_data = client.get(
            "/inventory_levels.json",
            {"inventory_item_ids": inventory_item_id, "location_ids": location_id},
        )
        levels = inv_data.get("inventory_levels", [])
        current_qty = levels[0]["available"] if levels else "unknown"

        preview = (
            f"PREVIEW — Inventory update\n"
            f"  inventory_item_id : {inventory_item_id}\n"
            f"  location_id       : {location_id}\n"
            f"  Current quantity  : {current_qty}\n"
            f"  New quantity      : {quantity}"
        )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        client.post(
            "/inventory_levels/set.json",
            {
                "inventory_item_id": int(inventory_item_id),
                "location_id": int(location_id),
                "available": quantity,
            },
        )
        log_write(
            "update_inventory",
            f"item={inventory_item_id} location={location_id} | {current_qty} → {quantity}",
        )
        return f"Done. {preview}"
