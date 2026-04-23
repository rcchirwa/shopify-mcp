"""
Inventory tools — read and set inventory levels.

update_inventory requires confirm=True.
"""

from mcp.server.fastmcp import FastMCP

from shopify_client import (
    ShopifyClient,
    extract_user_errors,
    format_user_errors,
    from_gid,
    to_gid,
    with_confirm_hint,
)
from tools._filters import filter_variant_targets
from tools._log import log_write

# GET_PRODUCT_INVENTORY fetches `variants(first: 50)` — Shopify returns up to
# this many variants per request and silently truncates the rest. When a read
# hits this cap we warn the operator so bulk writes don't miss variants.
_VARIANTS_PAGE_CAP = 50

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


def _tracked_display(current):
    # Shopify omits `tracked` on variants without an InventoryItem; render
    # that as "unset" so the preview reads "unset → True" instead of the
    # literal "None → True".
    return "unset" if current is None else current


def _variant_label(v: dict) -> str:
    # Fall back to the numeric variant id when a variant has no title,
    # so the Failed list stays actionable instead of printing "None".
    return v.get("title") or f"variant {from_gid(v.get('id', ''))}"


def _pair_prefix(variant: dict, level: dict, loc_gid: str | None) -> str:
    """Render the leading identity segment of a (variant, location) pair line."""
    loc = level.get("location") or {}
    loc_name = loc.get("name") or f"id {from_gid(loc_gid or '')}"
    return (
        f"    • {variant['title']} @ {loc_name} — "
        f"variant_id: {from_gid(variant['id'])}, "
        f"location: {from_gid(loc_gid or '')}"
    )


def _current_display(current):
    """Preserve 0 as a legit current qty; render missing as 'N/A'."""
    return "N/A" if current is None else current


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
        data = client.execute(
            GET_INVENTORY_ITEM, {"id": to_gid("InventoryItem", inventory_item_id)}
        )
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
            return with_confirm_hint(preview)

        result = client.execute(
            SET_INVENTORY,
            {
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
            },
        )
        err = format_user_errors(result, "inventorySetOnHandQuantities")
        if err:
            return err

        log_write(
            "update_inventory",
            f"item={inventory_item_id} location={location_id} | {current_qty} → {quantity}",
        )
        return f"Done. {preview}"

    @server.tool()
    def update_variant_inventory_tracking(
        product_id: str,
        tracked: bool,
        variant_ids: list[str] | None = None,
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

        targets, unresolved = filter_variant_targets(variant_ids, variants)

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

        change_lines = (
            "\n".join(
                _variant_line(
                    v,
                    f"{_tracked_display((v.get('inventoryItem') or {}).get('tracked'))} → {tracked}",
                )
                for v in to_change
            )
            or "    (none)"
        )
        unchanged_lines = (
            "\n".join(_variant_line(v, f"already tracked={tracked}") for v in unchanged)
            or "    (none)"
        )
        unresolved_block = (
            ("\n  Unresolved variant ids:\n" + "\n".join(f"    • {vid}" for vid in unresolved))
            if unresolved
            else ""
        )

        preview = (
            f"PREVIEW — Variant inventory tracking update\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Target  : tracked={tracked}\n"
            f"  Would change ({len(to_change)}):\n{change_lines}\n"
            f"  Unchanged ({len(unchanged)}):\n{unchanged_lines}"
            f"{unresolved_block}"
        )

        if not confirm:
            return with_confirm_hint(preview)

        changed = []
        failed = []
        for v in to_change:
            inv_item_gid = (v.get("inventoryItem") or {}).get("id")
            if not inv_item_gid:
                failed.append(
                    {
                        "variant": _variant_label(v),
                        "error": "variant has no inventoryItem id",
                    }
                )
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
                failed.append(
                    {
                        "variant": _variant_label(v),
                        "error": f"transport error: {e}",
                    }
                )
                continue
            user_errors = extract_user_errors(result, "inventoryItemUpdate")
            if user_errors:
                msgs = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in user_errors)
                failed.append({"variant": _variant_label(v), "error": msgs})
            else:
                changed.append(v)

        changed_lines = (
            "\n".join(_variant_line(v, f"tracked={tracked}") for v in changed) or "    (none)"
        )
        failed_block = ""
        if failed:
            failed_block = f"\n  Failed ({len(failed)}):\n" + "\n".join(
                f"    • {f['variant']}: {f['error']}" for f in failed
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

    @server.tool()
    def update_variant_inventory_quantity(
        product_id: str,
        quantity: int,
        location_id: str | None = None,
        variant_ids: list[str] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Set inventory quantity on product variants at one or more locations.
        When variant_ids is omitted, applies to every variant of the product.
        When location_id is omitted, applies to every location each variant
        has a level at. A single `quantity` is applied to every matched
        (variant, location) pair via one inventorySetOnHandQuantities call.
        Returns a preview unless confirm=True.
        """
        product_gid = to_gid("Product", product_id)
        data = client.execute(GET_PRODUCT_INVENTORY, {"id": product_gid})
        product = data.get("product")
        if not product:
            return f"No product found with id {product_id}."

        variants = (product.get("variants") or {}).get("nodes", []) or []
        title = product.get("title", "")
        at_cap_warning = (
            (
                f"  WARNING: variant read hit the {_VARIANTS_PAGE_CAP}-variant page "
                f"cap — additional variants (if any) are not covered by this call."
            )
            if len(variants) >= _VARIANTS_PAGE_CAP
            else ""
        )

        targets, unresolved_variants = filter_variant_targets(variant_ids, variants)

        # Build (variant, level) pairs honoring the optional location filter.
        # `location_resolved_for_any_variant` tracks whether location_id matched
        # at least one level, so we can warn when the caller's filter misses
        # every variant.
        target_location_gid = to_gid("Location", location_id) if location_id else None
        pairs = []  # list of (variant, level, current_qty, location_gid)
        location_resolved_for_any_variant = False
        for v in targets:
            inv_item = v.get("inventoryItem") or {}
            levels = (inv_item.get("inventoryLevels") or {}).get("nodes", []) or []
            for lv in levels:
                loc_gid = (lv.get("location") or {}).get("id")
                if target_location_gid and loc_gid != target_location_gid:
                    continue
                if target_location_gid:
                    location_resolved_for_any_variant = True
                current_qty = _available_qty(lv)
                pairs.append((v, lv, current_qty, loc_gid))

        # Split pairs into three disjoint buckets:
        #   to_write  — needs change AND has valid inventoryItem.id + location.id
        #   skipped   — needs change BUT missing one of those ids (would poison
        #               the whole setQuantities batch with a Shopify validation
        #               error, so we drop them before the mutation and report
        #               them separately)
        #   unchanged — current qty already matches target
        # `current_qty is None` is treated as "needs change" — the safer default
        # than silently skipping, since the caller asked to set a target value.
        to_write = []
        skipped = []
        unchanged = []
        for pair in pairs:
            if pair[2] == quantity:
                unchanged.append(pair)
                continue
            v, _lv, _cur, loc_gid = pair
            inv_item_gid = (v.get("inventoryItem") or {}).get("id")
            if not inv_item_gid or not loc_gid:
                skipped.append(pair)
            else:
                to_write.append(pair)

        unresolved_location_block = ""
        if target_location_gid and not location_resolved_for_any_variant and targets:
            unresolved_location_block = (
                f"\n  Unresolved location: {location_id} "
                f"(not found on any of the targeted variants)"
            )

        write_lines = (
            "\n".join(
                f"{_pair_prefix(v, lv, loc)} — {_current_display(cur)} → {quantity}"
                for v, lv, cur, loc in to_write
            )
            or "    (none)"
        )
        unchanged_lines = (
            "\n".join(
                f"{_pair_prefix(v, lv, loc)} — already at {quantity}"
                for v, lv, cur, loc in unchanged
            )
            or "    (none)"
        )
        skipped_block = ""
        if skipped:
            skipped_lines = "\n".join(
                f"{_pair_prefix(v, lv, loc)} — missing inventoryItem.id or location.id, not written"
                for v, lv, _cur, loc in skipped
            )
            skipped_block = f"\n  Skipped ({len(skipped)}):\n{skipped_lines}"
        unresolved_variants_block = (
            (
                "\n  Unresolved variant ids:\n"
                + "\n".join(f"    • {vid}" for vid in unresolved_variants)
            )
            if unresolved_variants
            else ""
        )
        warning_block = f"\n{at_cap_warning}" if at_cap_warning else ""

        target_loc_label = location_id or "all locations"
        preview = (
            f"PREVIEW — Variant inventory quantity update\n"
            f"  Product     : {title} (id: {product_id})\n"
            f"  Target qty  : {quantity}\n"
            f"  Target loc  : {target_loc_label}\n"
            f"  Would change ({len(to_write)}):\n{write_lines}\n"
            f"  Unchanged ({len(unchanged)}):\n{unchanged_lines}"
            f"{skipped_block}"
            f"{unresolved_variants_block}"
            f"{unresolved_location_block}"
            f"{warning_block}"
        )

        if not confirm:
            return with_confirm_hint(preview)

        # Fast path: nothing writable. Happens when every pair is already at
        # the target, or the only pairs needing change all had missing ids.
        # Return before issuing a mutation with an empty setQuantities array
        # (Shopify rejects that).
        if not to_write:
            log_write(
                "update_variant_inventory_quantity",
                f"product={product_id} qty={quantity} location={location_id or 'all'} "
                f"written=0 unchanged={len(unchanged)} skipped={len(skipped)} "
                f"unresolved_variants={len(unresolved_variants)}",
            )
            return (
                f"CONFIRMED — Variant inventory quantity update (no-op)\n"
                f"  Product     : {title} (id: {product_id})\n"
                f"  Target qty  : {quantity}\n"
                f"  Target loc  : {target_loc_label}\n"
                f"  Changed     : (none — nothing writable at target)\n"
                f"  Unchanged ({len(unchanged)}):\n{unchanged_lines}"
                f"{skipped_block}"
                f"{unresolved_variants_block}"
                f"{unresolved_location_block}"
                f"{warning_block}"
            )

        # One round-trip: inventorySetOnHandQuantities accepts an array of
        # {inventoryItemId, locationId, quantity} entries, so the whole batch
        # goes through a single mutation call.
        set_quantities = [
            {
                "inventoryItemId": (v.get("inventoryItem") or {})["id"],
                "locationId": loc_gid,
                "quantity": quantity,
            }
            for v, _lv, _cur, loc_gid in to_write
        ]

        result = client.execute(
            SET_INVENTORY,
            {
                "input": {
                    "reason": "correction",
                    "setQuantities": set_quantities,
                }
            },
        )
        err = format_user_errors(result, "inventorySetOnHandQuantities")
        if err:
            return err

        changed_lines = "\n".join(
            f"{_pair_prefix(v, lv, loc)} — {_current_display(cur)} → {quantity}"
            for v, lv, cur, loc in to_write
        )

        log_write(
            "update_variant_inventory_quantity",
            f"product={product_id} qty={quantity} location={location_id or 'all'} "
            f"written={len(to_write)} unchanged={len(unchanged)} skipped={len(skipped)} "
            f"unresolved_variants={len(unresolved_variants)}",
        )
        return (
            f"CONFIRMED — Variant inventory quantity update\n"
            f"  Product     : {title} (id: {product_id})\n"
            f"  Target qty  : {quantity}\n"
            f"  Target loc  : {target_loc_label}\n"
            f"  Changed ({len(to_write)}):\n{changed_lines}\n"
            f"  Unchanged ({len(unchanged)}):\n{unchanged_lines}"
            f"{skipped_block}"
            f"{unresolved_variants_block}"
            f"{unresolved_location_block}"
            f"{warning_block}"
        )
