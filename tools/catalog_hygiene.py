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
    format_user_errors,
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

# ---------------------------------------------------------------------------
# GraphQL — Story 9.1 (update_product_category)
# ---------------------------------------------------------------------------

# Look up a Product when only its `handle` is known. Story 9.1 accepts numeric
# ID / GID / handle for `productId`; the first two short-circuit via to_gid,
# the handle path needs a query.
GET_PRODUCT_BY_HANDLE_MIN = """
query GetProductByHandleMin($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    category { id fullName name }
  }
}
"""

# Read current category for idempotency check + post-write snapshot. Narrower
# than tools.products.GET_PRODUCT_BY_ID — we only need id/title/category here.
GET_PRODUCT_CATEGORY = """
query GetProductCategory($id: ID!) {
  product(id: $id) {
    id
    title
    category { id fullName name }
  }
}
"""

# Shopify's Standard Product Taxonomy search. Returns up to 10 ranked nodes —
# rank is the order Shopify returns; there is no explicit `score` field.
TAXONOMY_SEARCH = """
query taxonomyCategories($search: String!) {
  taxonomy {
    categories(search: $search, first: 10) {
      nodes { id fullName name level isLeaf isRoot }
    }
  }
}
"""

# 2025+ Admin API takes `ProductUpdateInput` (singular `product:` argument);
# the legacy `ProductInput` shape is what `tools/products.py` still uses for
# title/handle/seo writes. Story 9.1 spec pins the new shape — keep them
# distinct so a future API-version bump doesn't have to untangle them.
UPDATE_PRODUCT_CATEGORY = """
mutation productUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product {
      id
      title
      category { id fullName name }
    }
    userErrors { field message }
  }
}
"""

_VALID_RESOLVE_STRATEGIES = ("exact", "best-match", "reject-ambiguous")
_TAXONOMY_GID_PREFIX = "gid://shopify/TaxonomyCategory/"
_PRODUCT_GID_PREFIX = "gid://shopify/Product/"


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


# ---------------------------------------------------------------------------
# Helpers — Story 9.1 (update_product_category)
# ---------------------------------------------------------------------------


def _format_payload(
    header: str,
    payload: dict[str, Any],
    *,
    confirm_hint: bool,
) -> str:
    """Render the hybrid string-preview + JSON-tail output for Story 9.1.

    `header` is the human-readable section above the JSON. `payload` is the
    structured response dict per the spec's "Return shape". `confirm_hint`
    controls whether the trailing "Reply with confirm=True…" line appears
    (only in the preview branch). Distinct from `_render` (Story 9.3) since
    the two tools emit slightly different head/tail compositions.
    """
    body = header
    if confirm_hint:
        body += "\n\nReply with confirm=True to execute."
    return f"{body}\n\n```json\n{json.dumps(payload)}\n```"


def _resolve_product_gid(
    client: ShopifyClient,
    product_id: str,
) -> tuple[str | None, str | None]:
    """Map a numeric ID / GID / handle to a Product GID.

    Returns (gid, error). On success, error is None. On failure (handle not
    found, malformed input, transport failure), gid is None and error is a
    human-readable string. Numeric and GID inputs short-circuit without a
    network call; handle inputs trigger a `productByHandle` query (wrapped
    in try/except so non-200 HTTP surfaces as a structured error per AC #8).
    """
    if not isinstance(product_id, str) or not product_id.strip():
        return None, "product_id must be a non-empty string."
    stripped = product_id.strip()

    if stripped.startswith(_PRODUCT_GID_PREFIX):
        if not stripped[len(_PRODUCT_GID_PREFIX) :]:
            return None, f"Empty product GID body: {stripped!r}"
        return stripped, None

    if stripped.isdigit():
        return to_gid("Product", stripped), None

    # Treat anything else as a handle. Shopify handles are lowercase
    # alphanumerics/hyphens/underscores — but rather than gate here, we let
    # the GraphQL query decide (returns null for unknown handles).
    try:
        data = client.execute(GET_PRODUCT_BY_HANDLE_MIN, {"handle": stripped})
    except Exception as e:
        return None, f"Handle lookup failed ({type(e).__name__}): {e}"
    product = (data or {}).get("productByHandle")
    if not product:
        return None, f"No product found with handle {stripped!r}."
    return product["id"], None


def _resolve_taxonomy_category(
    client: ShopifyClient,
    category: str,
    resolve_strategy: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """Resolve a free-text search string (or GID passthrough) to a category node.

    Returns (chosen_node, alternates, error). `chosen_node` is the dict
    {id, fullName, name, ...} that will be sent to productUpdate. `alternates`
    are runner-up leaves (only populated under best-match). When a
    TaxonomyCategory GID is supplied, no search is performed: chosen_node has
    only the `id` set so the caller still sees a stable shape.
    """
    if not isinstance(category, str) or not category.strip():
        return None, [], "category must be a non-empty string."
    stripped = category.strip()

    if stripped.startswith(_TAXONOMY_GID_PREFIX):
        if not stripped[len(_TAXONOMY_GID_PREFIX) :]:
            return None, [], f"Empty TaxonomyCategory GID body: {stripped!r}"
        # GID passthrough — caller already knows which leaf they want. We don't
        # fabricate a fullName; the post-write snapshot from productUpdate fills it.
        return {"id": stripped, "fullName": None, "name": None}, [], None

    # Ordering: taxonomy search runs before the product read (on the caller
    # side) so a 0-result / ambiguous / no-exact-match failure bails CHEAP,
    # without paying for an unused product fetch. The trade-off: a transport
    # failure here happens before we've validated the product exists. The
    # try/except below makes that surface as a structured error (AC #8),
    # mirroring the product-read and mutation wrappers in the caller.
    try:
        data = client.execute(TAXONOMY_SEARCH, {"search": stripped})
    except Exception as e:
        return None, [], f"Taxonomy search failed ({type(e).__name__}): {e}"
    nodes = ((data or {}).get("taxonomy") or {}).get("categories", {}).get("nodes") or []
    leaves = [n for n in nodes if n.get("isLeaf")]
    if not leaves:
        return (
            None,
            [],
            f"No taxonomy categories matched search {stripped!r}. Try a broader term.",
        )

    if resolve_strategy == "exact":
        # Match by fullName OR short name — intentional. Callers paste either
        # the leaf's short label ("Sweatshirts") or its full path
        # ("Apparel & Accessories > … > Sweatshirts"); both should resolve.
        # Defensive: two distinct leaves matching the same needle (one by
        # fullName, one by name) still trip the >1 guard below.
        needle = stripped.casefold()
        matches = [
            n
            for n in leaves
            if (n.get("fullName") or "").casefold() == needle
            or (n.get("name") or "").casefold() == needle
        ]
        if len(matches) == 0:
            return (
                None,
                [],
                f"resolve_strategy='exact' but no leaf category matched "
                f"{stripped!r} by fullName or name.",
            )
        if len(matches) > 1:
            return (
                None,
                [],
                f"resolve_strategy='exact' but {len(matches)} leaf categories "
                f"matched {stripped!r} exactly — refine the search.",
            )
        return matches[0], [], None

    if resolve_strategy == "reject-ambiguous":
        if len(leaves) > 1:
            return (
                None,
                [],
                f"resolve_strategy='reject-ambiguous' but {len(leaves)} leaf "
                f"categories matched {stripped!r} — refine the search or use "
                f"resolve_strategy='best-match'.",
            )
        return leaves[0], [], None

    # best-match: first leaf wins; subsequent leaves become alternates in
    # Shopify's relevance order. No `score` field — Shopify doesn't return
    # one, and synthesizing a rank-based stand-in misled downstream agents
    # into treating it as a probability. Order alone carries the signal.
    chosen = leaves[0]
    alternates = [
        {
            "id": n["id"],
            "fullName": n.get("fullName"),
            "name": n.get("name"),
        }
        for n in leaves[1:]
    ]
    return chosen, alternates, None


def _build_payload(
    *,
    ok: bool,
    product: dict[str, Any] | None,
    alternates: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    preview: bool,
) -> dict[str, Any]:
    """Construct the JSON-tail payload in the spec's documented shape."""
    return {
        "ok": ok,
        "product": product,
        "alternates": alternates,
        "errors": errors,
        "preview": preview,
    }


def _shape_product_snapshot(product_node: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a raw product node into the spec's `{id, title, category}` shape."""
    if not product_node:
        return None
    category = product_node.get("category") or None
    return {
        "id": product_node.get("id"),
        "title": product_node.get("title"),
        "category": category
        and {
            "id": category.get("id"),
            "fullName": category.get("fullName"),
            "name": category.get("name"),
        },
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

    @server.tool()
    def update_product_category(
        product_id: str,
        category: str,
        resolve_strategy: str = "best-match",
        confirm: bool = False,
    ) -> str:
        """
        Set or change a product's Standard Product Taxonomy category.

        `product_id` accepts numeric ID, GID, or handle.
        `category` accepts a TaxonomyCategory GID or a free-text search string.
        `resolve_strategy` ∈ {"exact", "best-match", "reject-ambiguous"} — only
        meaningful when `category` is a search string; ignored for GID inputs.

        Returns a preview unless confirm=True. On success, the output is a
        human-readable summary followed by a fenced ```json ...``` block
        carrying the spec's structured return shape (ok, product, alternates,
        errors, preview).
        """
        # --- Validate resolve_strategy up front: cheaper than after a fetch.
        if resolve_strategy not in _VALID_RESOLVE_STRATEGIES:
            payload = _build_payload(
                ok=False,
                product=None,
                alternates=[],
                errors=[
                    {
                        "message": (
                            f"Invalid resolve_strategy {resolve_strategy!r}. "
                            f"Must be one of: {', '.join(_VALID_RESOLVE_STRATEGIES)}."
                        )
                    }
                ],
                preview=not confirm,
            )
            return _format_payload(
                f"Error — update_product_category\n"
                f"  Invalid resolve_strategy: {resolve_strategy!r}\n"
                f"  Allowed: {', '.join(_VALID_RESOLVE_STRATEGIES)}",
                payload,
                confirm_hint=False,
            )

        # --- Resolve productId → Product GID (numeric / GID / handle).
        product_gid, prod_err = _resolve_product_gid(client, product_id)
        if prod_err or not product_gid:
            return _format_payload(
                f"Error — update_product_category\n  {prod_err}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=[],
                    errors=[
                        {
                            "message": prod_err or "product resolve failed",
                            "stage": "product-resolve",
                        }
                    ],
                    preview=not confirm,
                ),
                confirm_hint=False,
            )

        # --- Resolve category (GID passthrough OR taxonomy search).
        chosen, alternates, cat_err = _resolve_taxonomy_category(client, category, resolve_strategy)
        if cat_err or not chosen:
            return _format_payload(
                f"Error — update_product_category\n  {cat_err}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=alternates,
                    errors=[
                        {
                            "message": cat_err or "category resolve failed",
                            "stage": "category-resolve",
                        }
                    ],
                    preview=not confirm,
                ),
                confirm_hint=False,
            )

        target_category_gid = chosen["id"]
        target_full_name = chosen.get("fullName")

        # --- Fetch current category for idempotency + preview context.
        try:
            current_data = client.execute(GET_PRODUCT_CATEGORY, {"id": product_gid})
        except Exception as e:
            return _format_payload(
                f"Error — update_product_category\n"
                f"  Failed to read current product ({type(e).__name__}): {e}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=alternates,
                    errors=[{"message": str(e), "stage": "product-read"}],
                    preview=not confirm,
                ),
                confirm_hint=False,
            )
        current_product = (current_data or {}).get("product") or {}
        current_category = current_product.get("category") or {}
        old_category_id = current_category.get("id")
        old_category_fullname = current_category.get("fullName")

        # --- Idempotent no-op: target already set.
        if old_category_id and old_category_id == target_category_gid:
            payload = _build_payload(
                ok=True,
                product=_shape_product_snapshot(current_product),
                alternates=alternates,
                errors=[],
                preview=False,
            )
            header = (
                f"Done. Update product category (no-op, already set)\n"
                f"  Product ID : {from_gid(product_gid)}\n"
                f"  Category   : {old_category_fullname or '(unknown)'} "
                f"({old_category_id})"
            )
            return _format_payload(header, payload, confirm_hint=False)

        # --- Build the human-readable header common to preview + done branches.
        old_block = (
            "(none)"
            if not old_category_id
            else f"{old_category_id} — {old_category_fullname or '(unknown)'}"
        )
        new_full = target_full_name or "(name resolved by Shopify on write)"
        alt_count = len(alternates)
        alt_line = (
            "Alternates : (none)"
            if alt_count == 0
            else f"Alternates : {alt_count} runner-up(s) (see JSON tail)"
        )

        header_body = (
            f"  Product ID : {from_gid(product_gid)}\n"
            f"  Old        : {old_block}\n"
            f"  New        : {new_full}\n"
            f"  Resolved   : {target_category_gid} (strategy={resolve_strategy})\n"
            f"  {alt_line}"
        )

        # --- Preview branch (confirm=False): no mutation call.
        if not confirm:
            # Synthesize the post-write product shape from current title + target
            # category so callers can inspect what *would* be written.
            preview_product = {
                "id": product_gid,
                "title": current_product.get("title"),
                "category": {
                    "id": target_category_gid,
                    "fullName": target_full_name,
                    "name": chosen.get("name"),
                },
            }
            payload = _build_payload(
                ok=True,
                product=preview_product,
                alternates=alternates,
                errors=[],
                preview=True,
            )
            return _format_payload(
                f"PREVIEW — Update product category\n{header_body}",
                payload,
                confirm_hint=True,
            )

        # --- Execute branch (confirm=True): call productUpdate.
        try:
            result = client.execute(
                UPDATE_PRODUCT_CATEGORY,
                {"product": {"id": product_gid, "category": target_category_gid}},
            )
        except Exception as e:
            return _format_payload(
                f"Error — update_product_category\n  Mutation failed ({type(e).__name__}): {e}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=alternates,
                    errors=[{"message": str(e), "stage": "product-update"}],
                    preview=False,
                ),
                confirm_hint=False,
            )

        user_err = format_user_errors(result, "productUpdate")
        if user_err:
            raw_errors = (result or {}).get("productUpdate", {}).get("userErrors") or []
            return _format_payload(
                f"Error — update_product_category\n  {user_err}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=alternates,
                    errors=raw_errors,
                    preview=False,
                ),
                confirm_hint=False,
            )

        updated_product = (result or {}).get("productUpdate", {}).get("product") or {}
        payload = _build_payload(
            ok=True,
            product=_shape_product_snapshot(updated_product),
            alternates=alternates,
            errors=[],
            preview=False,
        )
        log_write(
            "update_product_category",
            f"id={from_gid(product_gid)} | "
            f"'{old_category_fullname or '(none)'}' → "
            f"'{(updated_product.get('category') or {}).get('fullName') or target_full_name or target_category_gid}'",
        )
        return _format_payload(
            f"Done. Update product category\n{header_body}",
            payload,
            confirm_hint=False,
        )
