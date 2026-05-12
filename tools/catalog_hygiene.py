"""
Catalog-hygiene tools — Epic 9 (Stories 9.1-9.7).

Wave 0 (this file at story-open): empty `register()` skeleton + the convention
contract below. Each Wave 1/2 story plugs its tool function into `register()`
without touching shopify_mcp.py again.

Write-tool convention — `confirm`, not `dryRun`:
    Trello card ualcSqFq pre-start gate: the spec's `dryRun: boolean` wording is
    NOT followed here. All write tools in this module use
    `confirm: bool = False` to match the existing 22-tool codebase convention
    (see tools/products.py:303 for the canonical template). Same semantics —
    `confirm=False` returns a preview, `confirm=True` executes — just the
    name the rest of the codebase already uses.

Return shape — human-readable head + fenced ```json``` tail:
    Per the spec amendment at shopify-aon-mcp-catalog-tools-spec.md:48, every
    return path emits two things, in order:
      1. A human-readable line/block in the existing tools/products.py style
         (PREVIEW —, CONFIRMED —, Error:, "No product found …").
      2. A fenced ```json``` block containing the documented per-tool payload
         (`{ok, …, errors}`). Downstream agents parse this tail; humans /
         LLMs-in-the-loop read the head.
    Use `_render(head, payload)` for every return — it guarantees the dual
    output and keeps the JSON serialization consistent across tools.
"""

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_client import (
    ShopifyClient,
    extract_user_errors,
    from_gid,
    to_gid,
    with_confirm_hint,
)
from tools._log import log_write
from tools._resolvers import resolve_variant_ids_with_variants

# Page cap mirrors `productVariantsBulkUpdate`'s 250-variant window; same idiom
# as tools/products.py:247. A product hitting this cap would need paginated
# reads + chunked bulk updates, which Story 9.3 does not implement.
VARIANTS_PAGE_CAP = 250

GET_PRODUCT_VARIANTS_FOR_PRICING = """
query GetProductVariantsForPricing($id: ID!) {
  product(id: $id) {
    id
    title
    variants(first: 250) {
      nodes { id sku price compareAtPrice }
    }
  }
}
"""

UPDATE_PRODUCT_VARIANTS_PRICING = """
mutation UpdateProductVariantsPricing(
  $productId: ID!,
  $variants: [ProductVariantsBulkInput!]!
) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    product { id }
    productVariants { id sku price compareAtPrice }
    userErrors { field message }
  }
}
"""


def _render(head: str, payload: dict[str, Any]) -> str:
    """Format the dual-output return: human-readable head + fenced JSON tail.

    Every tool return in this module funnels through here so the head/tail
    contract stays consistent. `indent=2` keeps the JSON readable when an
    LLM relays the response inline; `sort_keys=False` preserves insertion
    order so callers can rely on `id` appearing before `sku` etc.
    """
    return f"{head}\n\n```json\n{json.dumps(payload, indent=2)}\n```"


def _err_payload(message: str) -> dict[str, Any]:
    """Build the JSON tail for a tool-side error (validation, resolver, etc.).

    Shape mirrors the success payload's `errors` slot but with `ok: false`
    and an empty `variants` list. Validation errors have no Shopify `field`
    path, so the entry carries `message` only.
    """
    return {"ok": False, "variants": [], "errors": [{"message": message}]}


def _parse_positive_decimal(raw: object) -> Decimal:
    """Parse a strict-positive monetary value with ≤ 2 decimal places.

    Rejects negatives, zero, non-strings, NaN/inf, and >2 decimal places.
    AC 3 says "positive decimals" — read strictly per the plan. Raises
    ValueError with a readable message; callers turn this into `Error: ...`.
    """
    if not isinstance(raw, str):
        raise ValueError(f"{raw!r} is not a string")
    stripped = raw.strip()
    if not stripped:
        raise ValueError("price/compareAtPrice must be a non-empty string")
    try:
        value = Decimal(stripped)
    except InvalidOperation as exc:
        raise ValueError(f"{raw!r} is not a positive decimal") from exc
    if not value.is_finite():
        raise ValueError(f"{raw!r} is not a positive decimal")
    if value <= 0:
        raise ValueError(f"{raw!r} is not a positive decimal")
    # Reject >2 decimal places via quantize-roundtrip: 49.99 → 49.99 (equal),
    # 49.999 → 50.00 (not equal). Equality on Decimal is value-based, so
    # 49.9 / 49.90 / 49.900 all compare equal to their 2-place quantization.
    if value != value.quantize(Decimal("0.01")):
        raise ValueError(f"{raw!r} has more than 2 decimal places")
    return value


def _normalize_entries(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate caller's variants[] and return normalized per-entry dicts.

    Key presence encodes intent for each field — downstream code uses
    `"<field>" in entry` to decide whether to touch it:
      variantId: str (always present, stripped)
      price: str (present only if caller supplied it; validated, stripped)
      compareAtPrice: str | None (present only if caller supplied; None
        means "clear the field", a string means "set to this value")

    Rejects: empty list, non-dict entries, missing variantId, entries with
    neither price nor compareAtPrice, invalid decimals, and duplicate
    variantId across entries (merging would be ambiguous — caller should
    coalesce upstream).

    Raises ValueError on any validation failure; the whole call is rejected.
    """
    if not isinstance(variants, list) or not variants:
        raise ValueError("variants must be a non-empty list")

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(variants):
        if not isinstance(entry, dict):
            raise ValueError(f"variants[{i}] must be an object")

        variant_id = entry.get("variantId")
        if not isinstance(variant_id, str) or not variant_id.strip():
            raise ValueError(f"variants[{i}].variantId is required")
        trimmed_id = variant_id.strip()
        if trimmed_id in seen_ids:
            raise ValueError(
                f"variants[{i}].variantId {trimmed_id!r} is a duplicate; "
                "merge price/compareAtPrice into a single entry"
            )
        seen_ids.add(trimmed_id)

        has_price = "price" in entry
        has_cap = "compareAtPrice" in entry
        if not has_price and not has_cap:
            raise ValueError(f"variants[{i}] must supply price or compareAtPrice")

        normalized: dict[str, Any] = {"variantId": trimmed_id}

        if has_price:
            try:
                _parse_positive_decimal(entry["price"])
            except ValueError as exc:
                raise ValueError(f"variants[{i}].price {exc}") from exc
            normalized["price"] = entry["price"].strip()

        if has_cap:
            raw_cap = entry["compareAtPrice"]
            if raw_cap is None:
                normalized["compareAtPrice"] = None  # explicit clear
            else:
                try:
                    _parse_positive_decimal(raw_cap)
                except ValueError as exc:
                    raise ValueError(f"variants[{i}].compareAtPrice {exc}") from exc
                normalized["compareAtPrice"] = raw_cap.strip()

        out.append(normalized)

    return out


def _format_old_new(label: str, old: Any, new: Any) -> str:
    """Render a single field's old → new line, normalizing None → '(cleared)'."""
    old_disp = "(none)" if old is None else str(old)
    new_disp = "(cleared)" if new is None else str(new)
    return f"      {label}: {old_disp} → {new_disp}"


def _entry_matches_existing(
    target_price: str | None,
    target_cap: Any,
    has_cap: bool,
    existing: dict[str, Any],
) -> bool:
    """True if this entry's requested state already matches the variant."""
    if target_price is not None and Decimal(target_price) != Decimal(existing.get("price") or "0"):
        return False
    if has_cap:
        existing_cap = existing.get("compareAtPrice")
        if target_cap is None:
            if existing_cap is not None:
                return False
        else:
            if existing_cap is None or Decimal(target_cap) != Decimal(existing_cap):
                return False
    return True


def _project_variant(entry: dict[str, Any], existing: dict[str, Any], gid: str) -> dict[str, Any]:
    """Build the projected post-state for a single resolved variant.

    Combines caller's targets with the existing read — unchanged fields
    keep their existing value; `compareAtPrice: None` from the caller maps
    to a JSON null in the tail (the spec's "clear" signal).
    """
    target_price = entry["price"] if "price" in entry else existing.get("price")
    target_cap: Any
    if "compareAtPrice" in entry:
        target_cap = entry["compareAtPrice"]  # None = cleared
    else:
        target_cap = existing.get("compareAtPrice")
    return {
        "id": gid,
        "sku": existing.get("sku"),
        "price": target_price,
        "compareAtPrice": target_cap,
    }


def register(server: FastMCP, client: ShopifyClient) -> None:
    """Register catalog-hygiene tools on the MCP server.

    Stories 9.1-9.7 each add one `@server.tool()` function to this body.
    """

    @server.tool()
    def update_product_pricing(
        product_id: str,
        variants: list[dict[str, Any]],
        confirm: bool = False,
    ) -> str:
        """
        Update price and/or compareAtPrice on one or more variants of a
        product via productVariantsBulkUpdate. Each entry needs `variantId`
        plus at least one of `price` / `compareAtPrice`. `compareAtPrice:
        None` clears the field. `variantId` resolves from numeric / GID /
        SKU; ambiguous SKU and duplicate variantId entries fail the call.
        Decimal validation rejects the whole call if any entry is invalid
        (no partial application). Returns a preview unless confirm=True.

        Return shape: human-readable head + fenced ```json``` block with
        `{ok, variants[{id, sku, price, compareAtPrice}], errors[]}` per
        the spec.

        product_id: numeric ID or GID. Handle-based resolution is not
        supported in Story 9.3 — pass a numeric ID or GID.
        """
        # ---- Validate inputs --------------------------------------------
        try:
            entries = _normalize_entries(variants)
        except ValueError as exc:
            return _render(f"Error: {exc}", _err_payload(str(exc)))

        product_gid = to_gid("Product", product_id)

        # ---- Read existing pricing state (one round-trip) ---------------
        # Wide read pulls id+sku+price+compareAtPrice in one call. The same
        # variants list feeds both SKU resolution and the old→new preview.
        data = client.execute(GET_PRODUCT_VARIANTS_FOR_PRICING, {"id": product_gid})
        product = (data or {}).get("product")
        if not product:
            msg = f"No product found with id {product_id}."
            return _render(msg, _err_payload(msg))

        title = product.get("title", "")
        all_variants = (product.get("variants") or {}).get("nodes", []) or []
        by_gid = {v["id"]: v for v in all_variants}
        at_cap_warning = (
            f"  WARNING: variant read hit the {VARIANTS_PAGE_CAP}-variant page "
            f"cap — additional variants (if any) are not covered by this call."
            if len(all_variants) >= VARIANTS_PAGE_CAP
            else ""
        )

        # ---- Resolve variantIds in-memory (no second fetch) -------------
        try:
            resolved_gids = resolve_variant_ids_with_variants(
                [e["variantId"] for e in entries],
                all_variants,
                product_gid=product_gid,
            )
        except ValueError as exc:
            return _render(f"Error: {exc}", _err_payload(str(exc)))

        # Two different raw inputs can resolve to the same variant (e.g.,
        # "201" and "SKU-A" if SKU-A's variant is 201). _normalize_entries
        # catches identical raw inputs; this catches the post-resolution
        # collision so a redundant payload never hits Shopify.
        seen_gids: set[str] = set()
        for gid, entry in zip(resolved_gids, entries, strict=True):
            if gid in seen_gids:
                msg = (
                    f"variantId {entry['variantId']!r} resolves to the "
                    f"same variant ({from_gid(gid)}) as an earlier entry; "
                    "merge price/compareAtPrice into a single entry"
                )
                return _render(f"Error: {msg}", _err_payload(msg))
            seen_gids.add(gid)

        # ---- Build preview, detect no-op -------------------------------
        preview_lines: list[str] = []
        projected_variants: list[dict[str, Any]] = []
        not_found_messages: list[str] = []
        all_match = True
        unknown_gids: list[str] = []
        for entry, gid in zip(entries, resolved_gids, strict=True):
            existing = by_gid.get(gid)
            if existing is None:
                # Numeric/GID short-circuits resolution without consulting the
                # variants list, so a caller-supplied ID that isn't on the
                # product slips through to here. Catch it instead of letting
                # Shopify reject after the network round-trip.
                unknown_gids.append(gid)
                all_match = False
                preview_lines.append(f"    • id: {from_gid(gid)} — (variant not found on product)")
                not_found_messages.append(
                    f"variant {from_gid(gid)} not found on product {product_id}"
                )
                continue

            target_price = entry.get("price")
            has_cap = "compareAtPrice" in entry
            target_cap = entry.get("compareAtPrice") if has_cap else None

            matches = _entry_matches_existing(target_price, target_cap, has_cap, existing)
            if not matches:
                all_match = False

            sku = existing.get("sku") or "(no SKU)"
            block_lines = [f"    • id: {from_gid(gid)} — SKU: {sku}"]
            if target_price is not None:
                block_lines.append(_format_old_new("price", existing.get("price"), target_price))
            if has_cap:
                block_lines.append(
                    _format_old_new("compareAtPrice", existing.get("compareAtPrice"), target_cap)
                )
            if matches:
                block_lines.append("      (already at target)")
            preview_lines.append("\n".join(block_lines))

            projected_variants.append(_project_variant(entry, existing, gid))

        warning_block = f"\n{at_cap_warning}" if at_cap_warning else ""
        preview = (
            f"PREVIEW — Product pricing update\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Targets ({len(entries)}):\n" + "\n".join(preview_lines) + warning_block
        )

        if not confirm:
            preview_payload: dict[str, Any] = {
                "ok": not unknown_gids,
                "variants": projected_variants,
                "errors": [{"message": m} for m in not_found_messages],
            }
            return with_confirm_hint(_render(preview, preview_payload))

        # ---- Reject if any resolved gid is unknown to the product -------
        if unknown_gids:
            head = "Error: resolved variant(s) not found on product: " + ", ".join(
                from_gid(g) for g in unknown_gids
            )
            return _render(
                head,
                {
                    "ok": False,
                    "variants": [],
                    "errors": [{"message": m} for m in not_found_messages],
                },
            )

        # ---- Idempotent fast-path --------------------------------------
        if all_match:
            log_write(
                "update_product_pricing",
                f"product={product_id} variants={len(entries)} no-op",
            )
            head = (
                f"CONFIRMED — Product pricing update (no-op)\n"
                f"  Product : {title} (id: {product_id})\n"
                f"  Targets : {len(entries)} (all already at target values)"
                f"{warning_block}"
            )
            return _render(
                head,
                {"ok": True, "variants": projected_variants, "errors": []},
            )

        # ---- Build mutation input + execute ----------------------------
        variants_input: list[dict[str, Any]] = []
        for entry, gid in zip(entries, resolved_gids, strict=True):
            payload: dict[str, Any] = {"id": gid}
            if "price" in entry:
                payload["price"] = entry["price"]
            if "compareAtPrice" in entry:
                payload["compareAtPrice"] = entry["compareAtPrice"]
            variants_input.append(payload)

        result = client.execute(
            UPDATE_PRODUCT_VARIANTS_PRICING,
            {"productId": product_gid, "variants": variants_input},
        )

        user_errors = extract_user_errors(result, "productVariantsBulkUpdate")
        if user_errors:
            # `field` is a dotted-path list on productVariantsBulkUpdate; mirror
            # the local formatter from tools/products.py:868 so paths render
            # readably (format_user_errors stringifies the whole list).
            def _fmt(e: dict[str, Any]) -> str:
                field_path = ".".join(str(f) for f in (e.get("field") or []))
                return f"{field_path or '(no field)'}: {e.get('message', '')}"

            msgs = "; ".join(_fmt(e) for e in user_errors)
            return _render(
                f"Error: {msgs}",
                {"ok": False, "variants": [], "errors": list(user_errors)},
            )

        updated = (result.get("productVariantsBulkUpdate") or {}).get("productVariants") or []
        updated_lines = (
            "\n".join(
                f"    • id: {from_gid(v['id'])} — SKU: {v.get('sku') or '(no SKU)'} — "
                f"price: {v.get('price')} — compareAtPrice: "
                f"{v.get('compareAtPrice') if v.get('compareAtPrice') is not None else '(cleared)'}"
                for v in updated
            )
            or "    (none returned)"
        )

        log_write(
            "update_product_pricing",
            f"product={product_id} variants={len(entries)}",
        )
        head = (
            f"CONFIRMED — Product pricing update\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Updated ({len(updated)}):\n{updated_lines}"
            f"{warning_block}"
        )
        return _render(head, {"ok": True, "variants": list(updated), "errors": []})
