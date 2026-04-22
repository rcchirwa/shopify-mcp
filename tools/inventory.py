"""
Inventory tools — read and set inventory levels.

update_inventory requires confirm=True.
"""

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient, to_gid, from_gid
from tools._log import log_write

# 2024-07+ replaced InventoryLevel.available with
# `quantities(names: [...]) { name quantity }`. The `available` name is the
# direct equivalent of the old field.
GET_PRODUCT_INVENTORY = """
query GetProductInventory($id: ID!) {
  product(id: $id) {
    title
    variants(first: 50) {
      nodes {
        id
        title
        sku
        inventoryItem {
          id
          inventoryLevels(first: 10) {
            nodes {
              quantities(names: ["available"]) { name quantity }
              location { id name }
            }
          }
        }
      }
    }
  }
}
"""

GET_INVENTORY_ITEM = """
query GetInventoryItem($id: ID!) {
  inventoryItem(id: $id) {
    inventoryLevels(first: 10) {
      nodes {
        quantities(names: ["available"]) { name quantity }
        location { id }
      }
    }
  }
}
"""


SET_INVENTORY = """
mutation SetInventory($input: InventorySetOnHandQuantitiesInput!) {
  inventorySetOnHandQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt }
    userErrors { field message }
  }
}
"""


def _available_qty(level: dict):
    """Extract the 'available' quantity from an InventoryLevel's quantities
    array (2024-07+ shape). Returns the integer quantity, or None if the
    `available` name wasn't returned."""
    for q in level.get("quantities") or []:
        if q.get("name") == "available":
            return q.get("quantity")
    return None


def register(server: FastMCP, client: ShopifyClient):

    @server.tool()
    def get_inventory(product_id: str) -> str:
        """Get inventory levels for a product and all its variants."""
        data = client.execute(GET_PRODUCT_INVENTORY, {"id": to_gid("Product", product_id)})
        product = data.get("product")
        if not product:
            return f"Product {product_id} not found."

        lines = [f"Inventory for: {product['title']} (id: {product_id})\n"]
        # `or {}` guards against Shopify returning `null` for any of these
        # nested fields — `.get(key, default)` wouldn't apply the default.
        for variant in (product.get("variants") or {}).get("nodes", []) or []:
            inv_item = variant.get("inventoryItem") or {}
            levels = (inv_item.get("inventoryLevels") or {}).get("nodes", []) or []
            # Preserve 0 as a legit qty — only fall back to "N/A" on missing.
            qty = _available_qty(levels[0]) if levels else None
            if qty is None:
                qty = "N/A"
            lines.append(
                f"  • {variant['title']} — SKU: {variant.get('sku', 'N/A')} "
                f"— available: {qty} — variant_id: {from_gid(variant['id'])}"
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
        # Fetch current level. `data.get("inventoryItem", {})` is not safe: if
        # Shopify returns `{"inventoryItem": null}` (deleted / wrong id), the
        # default isn't used and we'd crash on a None.get() chain. Same shape
        # elsewhere in this module — see get_inventory above.
        data = client.execute(GET_INVENTORY_ITEM, {"id": to_gid("InventoryItem", inventory_item_id)})
        inv_item = data.get("inventoryItem") or {}
        levels = (inv_item.get("inventoryLevels") or {}).get("nodes", [])
        location_gid = to_gid("Location", location_id)
        matching = [lv for lv in levels if lv.get("location", {}).get("id") == location_gid]
        # Preserve 0 as a legit current qty — only fall back to "unknown" on missing.
        current_qty = _available_qty(matching[0]) if matching else None
        if current_qty is None:
            current_qty = "unknown"

        preview = (
            f"PREVIEW — Inventory update\n"
            f"  inventory_item_id : {inventory_item_id}\n"
            f"  location_id       : {location_id}\n"
            f"  Current quantity  : {current_qty}\n"
            f"  New quantity      : {quantity}"
        )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        result = client.execute(SET_INVENTORY, {
            "input": {
                "reason": "correction",
                "setQuantities": [
                    {
                        "inventoryItemId": to_gid("InventoryItem", inventory_item_id),
                        "locationId": location_gid,
                        "quantity": quantity,
                    }
                ],
            }
        })
        user_errors = result.get("inventorySetOnHandQuantities", {}).get("userErrors", [])
        if user_errors:
            msgs = "; ".join(f"{e['field']}: {e['message']}" for e in user_errors)
            return f"Error: {msgs}"

        log_write(
            "update_inventory",
            f"item={inventory_item_id} location={location_id} | {current_qty} → {quantity}",
        )
        return f"Done. {preview}"
