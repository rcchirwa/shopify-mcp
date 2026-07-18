"""
Product tools — read and write Shopify products.

Thin MCP-tool surface over ``shopify.operations.products``: this module keeps
param coercion, the preview/confirm flow, and output formatting; the GraphQL
strings live in ``shopify.queries.products`` and the data access in
``shopify.operations.products`` (Story 10.23 / A5).

Write operations require confirm=True and log to aon_mcp_log.txt.
"""

import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify.operations import products as ops
from shopify.queries.products import (
    GET_PRODUCT_BY_HANDLE,
    GET_PRODUCT_BY_ID,
    GET_PRODUCT_COLLECTIONS,
    GET_PRODUCT_FULL_BY_HANDLE,
    GET_PRODUCT_FULL_BY_ID,
    GET_PRODUCT_SEO_BY_ID,
    GET_PRODUCT_VARIANTS_POLICY,
    GET_PRODUCTS,
    GET_PRODUCTS_BY_COLLECTION,
    GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS,
    GET_PRODUCTS_WITH_DESCRIPTIONS,
    UPDATE_PRODUCT,
    UPDATE_PRODUCT_STATUS,
    UPDATE_PRODUCT_TAGS,
    UPDATE_PRODUCT_VARIANTS_POLICY,
)
from shopify_client import ShopifyClient
from tools._filters import (
    filter_variant_targets,
    html_safety_findings,
    html_strip_report,
    sanitize_html,
)
from tools._gid import from_gid
from tools._log import log_write
from tools._response import extract_user_errors, with_confirm_hint
from tools._write_tool import write_gate
from validators.naming import format_validation_diff
from validators.seo import SEO_DESCRIPTION_MAX_CHARS, SEO_TITLE_MAX_CHARS

# The GraphQL strings now live in shopify.queries.products. They are re-exported
# here so existing callers/tests (`from tools.products import GET_PRODUCT_BY_ID`)
# keep resolving to the same objects the operations layer executes.
__all__ = [
    "GET_PRODUCTS",
    "GET_PRODUCTS_BY_COLLECTION",
    "GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS",
    "GET_PRODUCTS_WITH_DESCRIPTIONS",
    "GET_PRODUCT_BY_HANDLE",
    "GET_PRODUCT_BY_ID",
    "GET_PRODUCT_COLLECTIONS",
    "GET_PRODUCT_FULL_BY_HANDLE",
    "GET_PRODUCT_FULL_BY_ID",
    "GET_PRODUCT_SEO_BY_ID",
    "GET_PRODUCT_VARIANTS_POLICY",
    "UPDATE_PRODUCT",
    "UPDATE_PRODUCT_STATUS",
    "UPDATE_PRODUCT_TAGS",
    "UPDATE_PRODUCT_VARIANTS_POLICY",
    "register",
    "slugify_shopify_handle",
]


def slugify_shopify_handle(title: str) -> str:
    """Slugify a product title the way Shopify does when auto-generating a handle."""
    s = title.lower()
    s = re.sub(r'["\u201c\u201d\u2018\u2019\']', "", s)
    s = re.sub(r"[^a-z0-9\-_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


PRODUCT_STATUS_VALUES = ("ACTIVE", "DRAFT", "ARCHIVED")
INVENTORY_POLICY_VALUES = ("DENY", "CONTINUE")
TAG_MODES = ("replace", "append", "remove")


def register(server: FastMCP, client: ShopifyClient) -> None:

    @server.tool()
    def get_products() -> str:
        """List all products with id, title, handle, status, and variants."""
        products = ops.read_products(client)
        if not products:
            return "No products found."
        lines = []
        for p in products:
            variants = ", ".join(
                f"{v['title']} (id:{from_gid(v['id'])})"
                for v in p.get("variants", {}).get("nodes", [])
            )
            lines.append(
                f"[{from_gid(p['id'])}] {p['title']} | handle: {p['handle']} | status: {p['status']}\n"
                f"  Variants: {variants}"
            )
        return "\n\n".join(lines)

    @server.tool()
    def get_product(product_id: str = "", handle: str = "") -> str:
        """Get a single product by id or handle."""
        if not product_id and not handle:
            return "Provide either product_id or handle."
        p, variants_nodes, capped = ops.read_product(client, product_id=product_id, handle=handle)
        if not p:
            return "No product found."

        variants = "\n".join(
            f"  • {v['title']} — SKU: {v.get('sku', 'N/A')} — id: {from_gid(v['id'])}"
            for v in variants_nodes
        )
        result = (
            f"ID: {from_gid(p['id'])}\n"
            f"Title: {p['title']}\n"
            f"Handle: {p['handle']}\n"
            f"Status: {p['status']}\n"
            f"Variants:\n{variants}"
        )
        if capped:
            result += "\nWARNING: variant pagination hit the max-pages cap — additional variants (if any) are not shown here."
        return result

    @server.tool()
    def update_product_title(
        product_id: str,
        new_title: str,
        confirm: bool = False,
        change_handle: bool = False,
    ) -> str:
        """
        Update a product title. Returns a preview unless confirm=True.
        When change_handle=False the existing handle is explicitly preserved in
        the mutation so Shopify does not auto-regenerate it. When change_handle
        =True the new handle is the slugified new_title; if that slug matches
        the existing handle, the handle is effectively unchanged.
        """
        product = ops.fetch_product_core(client, product_id) or {}
        old_title = product.get("title", "")
        old_handle = product.get("handle", "")

        slugified = slugify_shopify_handle(new_title)
        if change_handle:
            target_handle = slugified
            if target_handle == old_handle:
                handle_block = f"  Handle     : UNCHANGED (new slug matches existing: {old_handle})"
            else:
                handle_block = f"  Old handle : {old_handle}\n  New handle : {target_handle}"
        else:
            target_handle = old_handle
            handle_block = "  Handle     : UNCHANGED (preserved; change_handle=False)"

        validation = format_validation_diff(old_title, new_title)

        preview = (
            f"PREVIEW — Product title update\n"
            f"  Product ID : {product_id}\n"
            f"  Old title  : {old_title}\n"
            f"  New title  : {new_title}\n"
            f"{handle_block}\n\n"
            f"Naming validation:\n{validation}"
        )

        return write_gate(
            preview=preview,
            confirm=confirm,
            execute=lambda: ops.update_product_title(client, product_id, new_title, target_handle),
            mutation_key="productUpdate",
            log_name="update_product_title",
            log_description=f"id={product_id} | '{old_title}' → '{new_title}' | handle '{old_handle}' → '{target_handle}'",
        )

    @server.tool()
    def update_product_description(
        product_id: str,
        new_description: str,
        confirm: bool = False,
    ) -> str:
        """
        Update a product's body_html description. Returns a preview unless confirm=True.
        """
        product = ops.fetch_product_core(client, product_id) or {}
        old_desc = product.get("bodyHtml", "")

        danger = html_safety_findings(new_description)
        warning_block = (
            (
                "\n\n⚠ DANGEROUS HTML DETECTED in new description:\n"
                + "\n".join(f"  • {p!r}" for p in danger)
                + "\nStorefront themes render descriptionHtml without escaping."
            )
            if danger
            else ""
        )

        sanitized_description = sanitize_html(new_description)
        stripped = html_strip_report(new_description)
        strip_block = (
            (
                "\n\n✂ CONTENT WILL BE SANITIZED — stripped before writing:\n"
                + "\n".join(f"  • {s}" for s in stripped)
                + "\nAllowed: p, br, b, i, em, strong, u, ul, ol, li, h1-h6, a[href,title], "
                "span/div[class], span[style: color/font-weight], img[src,alt], table/tr/td/th."
            )
            if stripped
            else ""
        )

        preview = (
            f"PREVIEW — Product description update\n"
            f"  Product ID   : {product_id}\n"
            f"  Old (excerpt): {old_desc[:120]}{'...' if len(old_desc) > 120 else ''}\n"
            f"  New (full)   :\n{new_description}" + warning_block + strip_block
        )

        return write_gate(
            preview=preview,
            confirm=confirm,
            execute=lambda: ops.update_product_description(
                client, product_id, sanitized_description
            ),
            mutation_key="productUpdate",
            log_name="update_product_description",
            log_description=f"id={product_id}",
            done_text=(
                "Done ✂ — disallowed HTML was stripped before writing. See preview for what changed."
                if stripped
                else "Done."
            )
            + f"\n{preview}",
        )

    @server.tool()
    def update_product_seo(
        product_id: str,
        new_seo_title: str = "",
        new_seo_description: str = "",
        confirm: bool = False,
    ) -> str:
        """
        Update a product's SEO title and/or meta description.
        At least one of new_seo_title or new_seo_description must be provided.
        Returns a preview unless confirm=True.
        """
        if not new_seo_title and not new_seo_description:
            return "Error: provide at least one of new_seo_title or new_seo_description."

        product = ops.read_product_seo(client, product_id)
        if not product:
            return f"No product found with id {product_id}."

        old_seo = product.get("seo") or {}
        old_title = old_seo.get("title") or ""
        old_desc = old_seo.get("description") or ""

        warnings = []
        if new_seo_title and len(new_seo_title) > SEO_TITLE_MAX_CHARS:
            warnings.append(
                f"SEO title is {len(new_seo_title)} chars "
                f"(> {SEO_TITLE_MAX_CHARS}, may be truncated in Google SERPs)"
            )
        if new_seo_description and len(new_seo_description) > SEO_DESCRIPTION_MAX_CHARS:
            warnings.append(
                f"SEO description is {len(new_seo_description)} chars "
                f"(> {SEO_DESCRIPTION_MAX_CHARS}, may be truncated in Google SERPs)"
            )

        seo_danger = html_safety_findings(new_seo_title or "") + html_safety_findings(
            new_seo_description or ""
        )
        if seo_danger:
            warnings.append(
                "⚠ DANGEROUS HTML pattern detected in SEO field(s): "
                + ", ".join(repr(p) for p in seo_danger)
                + " — SEO fields are rendered in <head>; verify intent."
            )

        sanitized_title = sanitize_html(new_seo_title) if new_seo_title else ""
        sanitized_description = sanitize_html(new_seo_description) if new_seo_description else ""
        title_stripped = html_strip_report(new_seo_title) if new_seo_title else []
        description_stripped = html_strip_report(new_seo_description) if new_seo_description else []
        if title_stripped:
            warnings.append(
                "✂ SEO title will be sanitized before writing — stripped: "
                + ", ".join(title_stripped)
            )
        if description_stripped:
            warnings.append(
                "✂ SEO description will be sanitized before writing — stripped: "
                + ", ".join(description_stripped)
            )

        old_title_line = old_title if old_title else "(empty)"
        old_desc_line = old_desc if old_desc else "(empty)"
        new_title_line = (
            f"{new_seo_title} ({len(new_seo_title)} chars)" if new_seo_title else "(unchanged)"
        )
        new_desc_line = (
            f"{new_seo_description} ({len(new_seo_description)} chars)"
            if new_seo_description
            else "(unchanged)"
        )

        body = (
            f"  Product ID          : {product_id}\n"
            f"  Old SEO title       : {old_title_line}\n"
            f"  New SEO title       : {new_title_line}\n"
            f"  Old SEO description : {old_desc_line}\n"
            f"  New SEO description : {new_desc_line}"
        )
        if warnings:
            body += "\n\nWarnings:\n" + "\n".join(f"  • {w}" for w in warnings)

        def _seo_input() -> dict[str, str]:
            inp: dict[str, str] = {}
            if new_seo_title:
                inp["title"] = sanitized_title
            if new_seo_description:
                inp["description"] = sanitized_description
            return inp

        def _log_desc() -> str:
            parts: list[str] = []
            if new_seo_title:
                parts.append(f"title: {len(old_title)} chars → {len(new_seo_title)} chars")
            if new_seo_description:
                parts.append(
                    f"description: {len(old_desc)} chars → {len(new_seo_description)} chars"
                )
            return f"id={product_id} | " + " | ".join(parts)

        return write_gate(
            preview=f"PREVIEW — Product SEO update\n{body}",
            confirm=confirm,
            execute=lambda: ops.update_product_seo(client, product_id, _seo_input()),
            mutation_key="productUpdate",
            log_name="update_product_seo",
            log_description=_log_desc,
            done_text=f"CONFIRMED — Product SEO updated\n{body}",
        )

    @server.tool()
    def get_products_by_collection(collection_handle: str) -> str:
        """List all products in a collection by collection handle."""
        col = ops.read_products_by_collection(client, collection_handle)
        if not col:
            return f"No collection found with handle '{collection_handle}'."

        products = col.get("products", {}).get("nodes", [])
        if not products:
            return f"No products in collection '{collection_handle}'."

        lines = [f"Products in '{collection_handle}' ({len(products)} total):\n"]
        for p in products:
            lines.append(
                f"  [{from_gid(p['id'])}] {p['title']} | handle: {p['handle']} | {p['status']}"
            )
        return "\n".join(lines)

    @server.tool()
    def get_product_description(product_id: str = "", handle: str = "") -> str:
        """Get the raw body_html description for a single product by id or handle."""
        if not product_id and not handle:
            return "Provide either product_id or handle."
        p = ops.read_product_description(client, product_id=product_id, handle=handle)
        if not p:
            return "No product found."

        return (
            f"ID: {from_gid(p['id'])}\n"
            f"Title: {p['title']}\n"
            f"Handle: {p['handle']}\n"
            f"body_html:\n{p.get('bodyHtml') or ''}"
        )

    @server.tool()
    def get_products_with_descriptions(collection_handle: str = "", limit: int = 50) -> str:
        """
        Bulk read product descriptions. If collection_handle is provided, scopes to that collection.
        Returns id, title, handle, status, and raw body_html for each product.
        """
        limit = max(1, min(limit, 250))

        if collection_handle:
            col = ops.read_collection_with_descriptions(client, collection_handle, limit)
            if not col:
                return f"No collection found with handle '{collection_handle}'."
            products = col.get("products", {}).get("nodes", [])
            header = f"Products in '{collection_handle}' ({len(products)} total):"
        else:
            products = ops.read_products_with_descriptions(client, limit=limit)
            header = f"Products ({len(products)} total):"

        if not products:
            return "No products found."

        blocks = [header]
        for p in products:
            blocks.append(
                f"\n---\n"
                f"ID: {from_gid(p['id'])}\n"
                f"Title: {p['title']}\n"
                f"Handle: {p['handle']}\n"
                f"Status: {p['status']}\n"
                f"body_html:\n{p.get('bodyHtml') or ''}"
            )
        return "\n".join(blocks)

    @server.tool()
    def get_product_full(product_id: str = "", handle: str = "") -> str:
        """
        Get a full product record: id, title, handle, status, body_html, tags,
        product_type, vendor, seo, category, variants, and options.
        """
        if not product_id and not handle:
            return "Provide either product_id or handle."
        p, variants_nodes, capped = ops.read_product_full(
            client, product_id=product_id, handle=handle
        )
        if not p:
            return "No product found."

        variants = "\n".join(
            f"  • {v['title']} — SKU: {v.get('sku', 'N/A')} — id: {from_gid(v['id'])}"
            for v in variants_nodes
        )
        tags = ", ".join(p.get("tags") or []) or "(none)"
        seo = p.get("seo") or {}
        seo_title = seo.get("title") or "(none)"
        seo_desc = seo.get("description") or "(none)"
        cat = p.get("category") or {}
        cat_id = cat.get("id") or "(none)"
        cat_name = cat.get("name") or "(none)"
        cat_full = cat.get("fullName") or "(none)"

        # Surface full GIDs (unlike the Variants block above, which uses from_gid)
        # so Story 9.5 callers can pipe them straight into productOptionUpdate.
        options_nodes = p.get("options") or []
        if options_nodes:
            option_lines: list[str] = []
            for opt in options_nodes:
                option_lines.append(f"  • {opt.get('name', '')} — id: {opt.get('id', '')}")
                for ov in opt.get("optionValues") or []:
                    option_lines.append(f"      - {ov.get('name', '')} — id: {ov.get('id', '')}")
            options_block = "\n".join(option_lines)
        else:
            options_block = "  (none)"

        result = (
            f"ID: {from_gid(p['id'])}\n"
            f"Title: {p['title']}\n"
            f"Handle: {p['handle']}\n"
            f"Status: {p['status']}\n"
            f"Product type: {p.get('productType') or '(none)'}\n"
            f"Vendor: {p.get('vendor') or '(none)'}\n"
            f"Tags: {tags}\n"
            f"SEO title: {seo_title}\n"
            f"SEO description: {seo_desc}\n"
            f"Category ID: {cat_id}\n"
            f"Category name: {cat_name}\n"
            f"Category full name: {cat_full}\n"
            f"Variants:\n{variants}\n"
            f"Options:\n{options_block}\n"
            f"body_html:\n{p.get('bodyHtml') or ''}"
        )
        if capped:
            result += "\nWARNING: variant pagination hit the max-pages cap — additional variants (if any) are not shown here."
        return result

    @server.tool()
    def get_product_collections(product_id: str) -> str:
        """List every collection this product belongs to — manual and smart."""
        product = ops.read_product_collections(client, product_id)
        if not product:
            return f"No product found with id {product_id}."

        collections = product.get("collections") or {}
        nodes = collections.get("nodes", []) or []
        has_more = (collections.get("pageInfo") or {}).get("hasNextPage", False)
        header = f"Product: {product.get('title', '')} (id: {product_id})"

        if not nodes:
            return f"{header}\nCollections (0 total): (none)"

        lines = [header, f"Collections ({len(nodes)} total):"]
        for c in nodes:
            # Match the type rule in tools/collections.py:_resolve_collection —
            # presence of ruleSet means smart (rule-driven), absence means manual.
            col_type = "smart" if c.get("ruleSet") else "manual"
            lines.append(
                f"  • {c.get('title', '')} (handle: {c.get('handle', '')}, "
                f"id: {from_gid(c.get('id', ''))}, type: {col_type})"
            )
        if has_more:
            lines.append(
                "  WARNING: product has more than 250 collection memberships — "
                "additional collections exist but are not listed here."
            )
        return "\n".join(lines)

    @server.tool()
    def update_product_tags(
        product_id: str,
        new_tags: list[str] | None = None,
        mode: str = "replace",
        confirm: bool = False,
    ) -> str:
        """
        Update a product's tags. Modes: replace (set tags verbatim — no pre-read),
        append (case-insensitive merge, existing casing wins), remove
        (case-insensitive strip). append and remove pre-read the product to
        compute the diff. Returns a preview unless confirm=True.
        """
        if mode not in TAG_MODES:
            return f"Error: mode must be one of {', '.join(TAG_MODES)}."
        if not new_tags:
            return "Error: new_tags must be a non-empty list."

        old_tags: list[str] = []
        if mode in ("append", "remove"):
            product = ops.fetch_product_full_record(client, product_id)
            if not product:
                return f"No product found with id {product_id}."
            old_tags = list(product.get("tags") or [])

        if mode == "replace":
            target = list(new_tags)
        elif mode == "append":
            # Case-insensitive dedup matching Shopify's server-side tag
            # normalization — existing casing wins on collision.
            target = list(old_tags)
            seen_lower = {t.lower() for t in target}
            for t in new_tags:
                if t.lower() not in seen_lower:
                    target.append(t)
                    seen_lower.add(t.lower())
        else:  # remove
            strip_lower = {t.lower() for t in new_tags}
            target = [t for t in old_tags if t.lower() not in strip_lower]

        if mode == "replace":
            body = (
                f"  Product ID : {product_id}\n"
                f"  Mode       : replace (no pre-read — overwrites verbatim)\n"
                f"  New tags   : {', '.join(target) if target else '(none)'}"
            )
        else:
            old_lower = {t.lower() for t in old_tags}
            new_lower = {t.lower() for t in target}
            added = [t for t in target if t.lower() not in old_lower]
            removed = [t for t in old_tags if t.lower() not in new_lower]
            body = (
                f"  Product ID : {product_id}\n"
                f"  Mode       : {mode}\n"
                f"  Old tags   : {', '.join(old_tags) if old_tags else '(none)'}\n"
                f"  New tags   : {', '.join(target) if target else '(none)'}\n"
                f"  Added      : {', '.join(added) if added else '(none)'}\n"
                f"  Removed    : {', '.join(removed) if removed else '(none)'}"
            )

        return write_gate(
            preview=f"PREVIEW — Product tags update\n{body}",
            confirm=confirm,
            execute=lambda: ops.update_product_tags(client, product_id, target),
            mutation_key="productUpdate",
            log_name="update_product_tags",
            log_description=f"id={product_id} mode={mode} | {len(old_tags)} tags → {len(target)} tags",
            done_text=f"CONFIRMED — Product tags updated\n{body}",
        )

    @server.tool()
    def update_product_status(
        product_id: str,
        new_status: str,
        confirm: bool = False,
    ) -> str:
        """
        Update a product's status: ACTIVE, DRAFT, or ARCHIVED. Reads current
        status for the preview. Returns a preview unless confirm=True.
        """
        if new_status not in PRODUCT_STATUS_VALUES:
            return f"Error: new_status must be one of {', '.join(PRODUCT_STATUS_VALUES)}."

        product = ops.fetch_product_core(client, product_id)
        if not product:
            return f"No product found with id {product_id}."
        old_status = product.get("status") or "(unknown)"

        no_op_suffix = "  (no-op — already at target status)" if old_status == new_status else ""
        body = (
            f"  Product ID : {product_id}\n"
            f"  Old status : {old_status}\n"
            f"  New status : {new_status}{no_op_suffix}"
        )

        return write_gate(
            preview=f"PREVIEW — Product status update\n{body}",
            confirm=confirm,
            execute=lambda: ops.update_product_status(client, product_id, new_status),
            mutation_key="productUpdate",
            log_name="update_product_status",
            log_description=f"id={product_id} | {old_status} → {new_status}",
            done_text=f"CONFIRMED — Product status updated\n{body}",
        )

    @server.tool()
    def update_variant_inventory_policy(
        product_id: str,
        new_policy: str,
        variant_ids: list[str] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Set inventoryPolicy (DENY or CONTINUE) on product variants via
        productVariantsBulkUpdate. When variant_ids is omitted, reads all
        variants of the product and applies the policy to each. When provided,
        filters to those variant ids; unknown ids are surfaced in the response
        and skipped. Returns a preview unless confirm=True.
        """
        if new_policy not in INVENTORY_POLICY_VALUES:
            return f"Error: new_policy must be one of {', '.join(INVENTORY_POLICY_VALUES)}."

        product, variants, capped = ops.read_product_variants_policy(client, product_id)
        if not product:
            return f"No product found with id {product_id}."

        title = product.get("title", "")
        at_cap_warning = (
            "  WARNING: variant pagination hit the max-pages cap — additional variants (if any) are not covered by this call."
            if capped
            else ""
        )

        targets, unresolved = filter_variant_targets(variant_ids, variants)

        target_lines = (
            "\n".join(
                f"    • {v['title']} — id: {from_gid(v['id'])} — "
                f"{v.get('inventoryPolicy', '(unknown)')} → {new_policy}"
                for v in targets
            )
            or "    (none)"
        )
        unresolved_block = (
            ("\n  Unresolved variant ids:\n" + "\n".join(f"    • {vid}" for vid in unresolved))
            if unresolved
            else ""
        )

        warning_block = f"\n{at_cap_warning}" if at_cap_warning else ""
        preview = (
            f"PREVIEW — Variant inventory policy update\n"
            f"  Product    : {title} (id: {product_id})\n"
            f"  New policy : {new_policy}\n"
            f"  Targets ({len(targets)}):\n{target_lines}"
            f"{unresolved_block}"
            f"{warning_block}"
        )

        if not confirm:
            return with_confirm_hint(preview)

        if not targets:
            body = (
                f"CONFIRMED — Variant inventory policy update (no-op)\n"
                f"  Product    : {title} (id: {product_id})\n"
                f"  New policy : {new_policy}\n"
                f"  Targets    : (none — nothing to update)"
                f"{unresolved_block}"
                f"{warning_block}"
            )
            log_write(
                "update_variant_inventory_policy",
                f"product={product_id} policy={new_policy} variants=0 unresolved={len(unresolved)}",
            )
            return body

        variants_input = [{"id": v["id"], "inventoryPolicy": new_policy} for v in targets]
        result = ops.update_variant_inventory_policy(client, product_id, variants_input)
        user_errors = extract_user_errors(result, "productVariantsBulkUpdate")
        if user_errors:
            # `field` is a dotted path list on productVariantsBulkUpdate (e.g.
            # ["variants", "0", "inventoryPolicy"]) — unlike simple scalar-field
            # mutations — so format_user_errors' stringify-field logic won't
            # render it readably. Keep the local formatter.
            def _fmt(e: dict[str, Any]) -> str:
                field_path = ".".join(str(f) for f in (e.get("field") or []))
                return f"{field_path or '(no field)'}: {e.get('message', '')}"

            msgs = "; ".join(_fmt(e) for e in user_errors)
            return f"Error: {msgs}"

        updated = (result.get("productVariantsBulkUpdate") or {}).get("productVariants") or []
        updated_lines = (
            "\n".join(
                f"    • id: {from_gid(v['id'])} — inventoryPolicy: {v.get('inventoryPolicy')}"
                for v in updated
            )
            or "    (none returned)"
        )

        log_write(
            "update_variant_inventory_policy",
            f"product={product_id} policy={new_policy} variants={len(targets)} "
            f"unresolved={len(unresolved)}",
        )
        return (
            f"CONFIRMED — Variant inventory policy update\n"
            f"  Product    : {title} (id: {product_id})\n"
            f"  New policy : {new_policy}\n"
            f"  Updated ({len(updated)}):\n{updated_lines}"
            f"{unresolved_block}"
            f"{warning_block}"
        )
