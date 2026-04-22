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
          tracked
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

# Per-variant toggle for InventoryItem.tracked. When tracked=false, Shopify's
# storefront reports the variant as available regardless of inventoryPolicy or
# quantity — the vault-product skill needs tracked=true before DENY + 0 take
# effect at the theme layer. Shopify Admin API 2024-10 exposes inventoryItemUpdate
# per item; no bulk variant taking a list of (id, input) pairs is documented in
# this API version, so the caller issues one mutation per variant.
UPDATE_INVENTORY_ITEM_TRACKED = """
mutation UpdateInventoryItemTracked($id: ID!, $input: InventoryItemInput!) {
  inventoryItemUpdate(id: $id, input: $input) {
    inventoryItem { id tracked }
    userErrors { field message }
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

    @server.tool()
    def update_variant_inventory_tracking(
        product_id: str,
        tracked: bool,
        variant_ids: list[str] = None,
        confirm: bool = False,
    ) -> str:
        """
        Toggle InventoryItem.tracked on product variants via inventoryItemUpdate.
        When variant_ids is omitted, reads all variants of the product and applies
        the target state to each. When provided, filters to those variant ids;
        unknown ids are surfaced in the response and skipped. Variants already at
        the target state are reported as unchanged and no mutation is issued for
        them. Returns a preview unless confirm=True.
        """
        product_gid = to_gid("Product", product_id)
        data = client.execute(GET_PRODUCT_INVENTORY, {"id": product_gid})
        product = data.get("product")
        if not product:
            return f"No product found with id {product_id}."

        variants = (product.get("variants") or {}).get("nodes", []) or []
        title = product.get("title", "")

        unresolved: list[str] = []
        if variant_ids:
            # Single pass over caller-supplied ids: preserves caller's order for
            # both targets and unresolved, and dedupes both sides (a caller that
            # passes the same id twice shouldn't see it mutated twice, nor
            # reported twice as unresolved).
            by_gid = {v["id"]: v for v in variants}
            targets = []
            seen_target_gids: set = set()
            seen_unresolved: set = set()
            for vid in variant_ids:
                gid = to_gid("ProductVariant", vid)
                if gid in by_gid:
                    if gid not in seen_target_gids:
                        targets.append(by_gid[gid])
                        seen_target_gids.add(gid)
                elif vid not in seen_unresolved:
                    unresolved.append(vid)
                    seen_unresolved.add(vid)
        else:
            targets = list(variants)

        # Split into "needs change" and "unchanged" based on current tracked state.
        to_change = []
        unchanged = []
        for v in targets:
            current = (v.get("inventoryItem") or {}).get("tracked")
            if current == tracked:
                unchanged.append(v)
            else:
                to_change.append(v)

        def _variant_line(v, suffix):
            inv_item = v.get("inventoryItem") or {}
            return (
                f"    • {v['title']} — variant_id: {from_gid(v['id'])}, "
                f"inventory_item: {from_gid(inv_item.get('id', ''))} — {suffix}"
            )

        change_lines = "\n".join(
            _variant_line(v, f"{(v.get('inventoryItem') or {}).get('tracked')} → {tracked}")
            for v in to_change
        ) or "    (none)"
        unchanged_lines = "\n".join(
            _variant_line(v, f"already tracked={tracked}") for v in unchanged
        ) or "    (none)"
        unresolved_block = (
            "\n  Unresolved variant ids:\n" +
            "\n".join(f"    • {vid}" for vid in unresolved)
        ) if unresolved else ""

        preview = (
            f"PREVIEW — Variant inventory tracking update\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Target  : tracked={tracked}\n"
            f"  Would change ({len(to_change)}):\n{change_lines}\n"
            f"  Unchanged ({len(unchanged)}):\n{unchanged_lines}"
            f"{unresolved_block}"
        )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        changed = []
        failed = []
        for v in to_change:
            inv_item_gid = (v.get("inventoryItem") or {}).get("id")
            if not inv_item_gid:
                failed.append({
                    "variant": v.get("title"),
                    "error": "variant has no inventoryItem id",
                })
                continue
            # Isolate transport errors per variant so a mid-loop failure doesn't
            # abort the batch — prior successes must still be reported and
            # logged, subsequent variants must still be attempted.
            try:
                result = client.execute(
                    UPDATE_INVENTORY_ITEM_TRACKED,
                    {"id": inv_item_gid, "input": {"tracked": tracked}},
                )
            except Exception as e:
                failed.append({
                    "variant": v.get("title"),
                    "error": f"transport error: {e}",
                })
                continue
            payload = result.get("inventoryItemUpdate", {}) or {}
            user_errors = payload.get("userErrors", []) or []
            if user_errors:
                msgs = "; ".join(
                    f"{e.get('field')}: {e.get('message')}" for e in user_errors
                )
                failed.append({"variant": v.get("title"), "error": msgs})
            else:
                changed.append(v)

        changed_lines = "\n".join(
            _variant_line(v, f"tracked={tracked}") for v in changed
        ) or "    (none)"
        failed_block = ""
        if failed:
            failed_block = (
                f"\n  Failed ({len(failed)}):\n" +
                "\n".join(f"    • {f['variant']}: {f['error']}" for f in failed)
            )

        log_write(
            "update_variant_inventory_tracking",
            f"product={product_id} target=tracked={tracked} "
            f"changed={[v['title'] for v in changed]} "
            f"unchanged={[v['title'] for v in unchanged]} "
            f"failed={len(failed)} unresolved={len(unresolved)}",
        )

        return (
            f"CONFIRMED — Variant inventory tracking update\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Target  : tracked={tracked}\n"
            f"  Changed ({len(changed)}):\n{changed_lines}\n"
            f"  Unchanged ({len(unchanged)}):\n{unchanged_lines}"
            f"{failed_block}"
            f"{unresolved_block}"
        )
