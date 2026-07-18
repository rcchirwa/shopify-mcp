"""
Inventory tools — read and set inventory levels.

Thin MCP-tool surface over ``shopify.operations.inventory``: this module keeps
param coercion, the (variant, location) bucketing, the preview/confirm flow, and
output formatting; the GraphQL strings live in ``shopify.queries.inventory`` and
the data access in ``shopify.operations.inventory`` (Story 10.28 / A5).

update_inventory requires confirm=True.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify.operations import inventory as ops
from shopify.queries.inventory import (
    GET_INVENTORY_ITEM,
    GET_PRODUCT_INVENTORY,
    SET_INVENTORY,
    UPDATE_INVENTORY_ITEM_TRACKED,
)
from shopify_client import ShopifyClient
from tools._filters import filter_variant_targets
from tools._gid import from_gid, to_gid
from tools._log import log_write
from tools._response import format_user_errors, format_user_errors_joined, with_confirm_hint
from tools._write_tool import write_gate

# The GraphQL strings now live in shopify.queries.inventory. They are re-exported
# here so existing callers/tests (`from tools.inventory import GET_PRODUCT_INVENTORY`)
# keep resolving to the same objects the operations layer executes.
__all__ = [
    "GET_INVENTORY_ITEM",
    "GET_PRODUCT_INVENTORY",
    "SET_INVENTORY",
    "UPDATE_INVENTORY_ITEM_TRACKED",
    "register",
]

# inventorySetOnHandQuantities sets an absolute on-hand quantity (not a delta),
# so negative values are never valid; Shopify's underlying field is a signed
# 32-bit int, so anything beyond int32 max would only fail server-side with an
# opaque error. Bound client-side instead (SEC-08).
INVENTORY_QTY_MAX = 2_147_483_647


def _available_qty(level: dict[str, Any]) -> int | None:
    """Extract the 'available' quantity from an InventoryLevel's quantities
    array (2024-07+ shape). Returns the integer quantity, or None if the
    `available` name wasn't returned."""
    for q in level.get("quantities") or []:
        if q.get("name") == "available":
            return q.get("quantity")
    return None


def _tracked_display(current: bool | None) -> bool | str:
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


def _current_display(current: int | None) -> int | str:
    """Preserve 0 as a legit current qty; render missing as 'N/A'."""
    return "N/A" if current is None else current


def _quantity_range_error(quantity: int) -> str | None:
    """Reject an out-of-range quantity before any Shopify call; None if valid."""
    if quantity < 0 or quantity > INVENTORY_QTY_MAX:
        return f"Error: quantity must be >= 0 and <= {INVENTORY_QTY_MAX} (got {quantity})."
    return None


def register(server: FastMCP, client: ShopifyClient) -> None:

    @server.tool()
    def get_inventory(product_id: str) -> str:
        """Get inventory levels for a product and all its variants."""
        product, variants, capped = ops.read_product_inventory(client, product_id)
        if not product:
            return f"Product {product_id} not found."

        lines = [f"Inventory for: {product['title']} (id: {product_id})\n"]
        for variant in variants:
            inv_item = variant.get("inventoryItem") or {}
            levels = (inv_item.get("inventoryLevels") or {}).get("nodes", []) or []
            # Preserve 0 as a legit qty — only fall back to "N/A" on missing.
            qty: int | str | None = _available_qty(levels[0]) if levels else None
            if qty is None:
                qty = "N/A"
            lines.append(
                f"  • {variant['title']} — SKU: {variant.get('sku', 'N/A')} "
                f"— available: {qty} — variant_id: {from_gid(variant['id'])}"
            )
        if capped:
            lines.append(
                "  WARNING: variant pagination hit the max-pages cap — "
                "additional variants (if any) are not shown here."
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
        quantity: must be >= 0 and <= 2,147,483,647 (int32 max).
        Returns a preview unless confirm=True.
        """
        range_err = _quantity_range_error(quantity)
        if range_err:
            return range_err

        # Fetch current level. `data.get("inventoryItem", {})` is not safe: if
        # Shopify returns `{"inventoryItem": null}` (deleted / wrong id), the
        # default isn't used and we'd crash on a None.get() chain. Same shape
        # elsewhere in this module — see get_inventory above.
        inv_item = ops.read_inventory_item_levels(client, inventory_item_id) or {}
        levels = (inv_item.get("inventoryLevels") or {}).get("nodes", [])
        location_gid = to_gid("Location", location_id)
        matching = [lv for lv in levels if lv.get("location", {}).get("id") == location_gid]
        # Preserve 0 as a legit current qty — only fall back to "unknown" on missing.
        current_qty: int | str | None = _available_qty(matching[0]) if matching else None
        if current_qty is None:
            current_qty = "unknown"

        preview = (
            f"PREVIEW — Inventory update\n"
            f"  inventory_item_id : {inventory_item_id}\n"
            f"  location_id       : {location_id}\n"
            f"  Current quantity  : {current_qty}\n"
            f"  New quantity      : {quantity}"
        )

        return write_gate(
            preview=preview,
            confirm=confirm,
            execute=lambda: ops.set_inventory_on_hand(
                client,
                [
                    {
                        "inventoryItemId": to_gid("InventoryItem", inventory_item_id),
                        "locationId": location_gid,
                        "quantity": quantity,
                    }
                ],
            ),
            mutation_key="inventorySetOnHandQuantities",
            log_name="update_inventory",
            log_description=f"item={inventory_item_id} location={location_id} | {current_qty} → {quantity}",
        )

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
        product, variants, capped = ops.read_product_inventory(client, product_id)
        if not product:
            return f"No product found with id {product_id}."
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

        def _variant_line(v: dict[str, Any], suffix: str) -> str:
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

        cap_block = (
            "\n  WARNING: variant pagination hit the max-pages cap — "
            "additional variants may not be covered."
            if capped
            else ""
        )

        preview = (
            f"PREVIEW — Variant inventory tracking update\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Target  : tracked={tracked}\n"
            f"  Would change ({len(to_change)}):\n{change_lines}\n"
            f"  Unchanged ({len(unchanged)}):\n{unchanged_lines}"
            f"{unresolved_block}"
            f"{cap_block}"
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
                result = ops.update_inventory_item_tracked(client, inv_item_gid, tracked)
            except Exception as e:
                failed.append(
                    {
                        "variant": _variant_label(v),
                        "error": f"transport error: {e}",
                    }
                )
                continue
            msgs = format_user_errors_joined(result, "inventoryItemUpdate")
            if msgs:
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
            f"{cap_block}"
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
        quantity: must be >= 0 and <= 2,147,483,647 (int32 max).
        Returns a preview unless confirm=True.
        """
        range_err = _quantity_range_error(quantity)
        if range_err:
            return range_err

        product, variants, capped = ops.read_product_inventory(client, product_id)
        if not product:
            return f"No product found with id {product_id}."
        title = product.get("title", "")
        at_cap_warning = (
            "  WARNING: variant pagination hit the max-pages cap — "
            "additional variants (if any) are not covered by this call."
            if capped
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

        result = ops.set_inventory_on_hand(client, set_quantities)
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
