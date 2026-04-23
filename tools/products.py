"""
Product tools — read and write Shopify products.

Write operations require confirm=True and log to aon_mcp_log.txt.
"""

import re

from mcp.server.fastmcp import FastMCP
from shopify_client import (
    ShopifyClient,
    extract_user_errors,
    format_user_errors,
    to_gid,
    from_gid,
)
from validators.naming import format_validation_diff
from tools._log import log_write
from tools._filters import filter_variant_targets

# SEO field length thresholds — above these, Google's SERP is likely to truncate.
# Sources: typical SERP pixel budget translates to ~70 chars for title and
# ~160 chars for meta description at default font/zoom.
SEO_TITLE_MAX_CHARS = 70
SEO_DESCRIPTION_MAX_CHARS = 160


def slugify_shopify_handle(title: str) -> str:
    """Slugify a product title the way Shopify does when auto-generating a handle."""
    s = title.lower()
    s = re.sub(r'["\u201c\u201d\u2018\u2019\']', '', s)
    s = re.sub(r'[^a-z0-9\-_]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s.strip('-')

GET_PRODUCTS = """
query GetProducts($first: Int!) {
  products(first: $first) {
    nodes {
      id
      title
      handle
      status
      variants(first: 50) {
        nodes { id title }
      }
    }
  }
}
"""

GET_PRODUCT_BY_ID = """
query GetProductById($id: ID!) {
  product(id: $id) {
    id
    title
    handle
    status
    bodyHtml
    variants(first: 50) {
      nodes { id title sku }
    }
  }
}
"""

GET_PRODUCT_BY_HANDLE = """
query GetProductByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    handle
    status
    bodyHtml
    variants(first: 50) {
      nodes { id title sku }
    }
  }
}
"""

UPDATE_PRODUCT = """
mutation UpdateProduct($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id title handle }
    userErrors { field message }
  }
}
"""

GET_PRODUCTS_BY_COLLECTION = """
query GetProductsByCollection($handle: String!, $first: Int!) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    products(first: $first) {
      nodes { id title handle status }
    }
  }
}
"""

GET_PRODUCTS_WITH_DESCRIPTIONS = """
query GetProductsWithDescriptions($first: Int!) {
  products(first: $first) {
    nodes {
      id
      title
      handle
      status
      bodyHtml
    }
  }
}
"""

GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS = """
query GetProductsByCollectionWithDescriptions($handle: String!, $first: Int!) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    products(first: $first) {
      nodes {
        id
        title
        handle
        status
        bodyHtml
      }
    }
  }
}
"""

GET_PRODUCT_FULL_BY_ID = """
query GetProductFullById($id: ID!) {
  product(id: $id) {
    id
    title
    handle
    status
    bodyHtml
    tags
    productType
    vendor
    seo { title description }
    variants(first: 50) {
      nodes { id title sku }
    }
  }
}
"""

GET_PRODUCT_FULL_BY_HANDLE = """
query GetProductFullByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    handle
    status
    bodyHtml
    tags
    productType
    vendor
    seo { title description }
    variants(first: 50) {
      nodes { id title sku }
    }
  }
}
"""

# Shopify caps this connection at 250 per request. A product with more memberships
# would need pagination; emit an at-cap warning so operators don't silently miss
# collections — the whole point of this tool is completeness on the vault path.
GET_PRODUCT_COLLECTIONS = """
query GetProductCollections($id: ID!) {
  product(id: $id) {
    id
    title
    collections(first: 250) {
      nodes {
        id
        handle
        title
        ruleSet { appliedDisjunctively }
      }
      pageInfo { hasNextPage }
    }
  }
}
"""

GET_PRODUCT_SEO_BY_ID = """
query GetProductSeoById($id: ID!) {
  product(id: $id) {
    id
    title
    seo { title description }
  }
}
"""

UPDATE_PRODUCT_TAGS = """
mutation UpdateProductTags($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id tags }
    userErrors { field message }
  }
}
"""

UPDATE_PRODUCT_STATUS = """
mutation UpdateProductStatus($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id status }
    userErrors { field message }
  }
}
"""

GET_PRODUCT_VARIANTS_POLICY = """
query GetProductVariantsPolicy($id: ID!) {
  product(id: $id) {
    id
    title
    variants(first: 250) {
      nodes { id title inventoryPolicy }
    }
  }
}
"""
# Shopify's per-request ceiling for the variants connection is 250; a product
# that actually hits this cap would need paginated reads + chunked bulk updates.
# Warn at-cap so operators can see they've hit a latent limit.
VARIANTS_PAGE_CAP = 250

UPDATE_PRODUCT_VARIANTS_POLICY = """
mutation UpdateProductVariantsPolicy($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    product { id }
    productVariants { id inventoryPolicy }
    userErrors { field message }
  }
}
"""

PRODUCT_STATUS_VALUES = ("ACTIVE", "DRAFT", "ARCHIVED")
INVENTORY_POLICY_VALUES = ("DENY", "CONTINUE")
TAG_MODES = ("replace", "append", "remove")


def register(server: FastMCP, client: ShopifyClient):

    @server.tool()
    def get_products() -> str:
        """List all products with id, title, handle, status, and variants."""
        data = client.execute(GET_PRODUCTS, {"first": 250})
        products = data.get("products", {}).get("nodes", [])
        if not products:
            return "No products found."
        lines = []
        for p in products:
            variants = ", ".join(
                f"{v['title']} (id:{from_gid(v['id'])})" for v in p.get("variants", {}).get("nodes", [])
            )
            lines.append(
                f"[{from_gid(p['id'])}] {p['title']} | handle: {p['handle']} | status: {p['status']}\n"
                f"  Variants: {variants}"
            )
        return "\n\n".join(lines)

    @server.tool()
    def get_product(product_id: str = "", handle: str = "") -> str:
        """Get a single product by id or handle."""
        if product_id:
            data = client.execute(GET_PRODUCT_BY_ID, {"id": to_gid("Product", product_id)})
            p = data.get("product")
        elif handle:
            data = client.execute(GET_PRODUCT_BY_HANDLE, {"handle": handle})
            p = data.get("productByHandle")
        else:
            return "Provide either product_id or handle."

        if not p:
            return f"No product found."

        variants = "\n".join(
            f"  • {v['title']} — SKU: {v.get('sku','N/A')} — id: {from_gid(v['id'])}"
            for v in p.get("variants", {}).get("nodes", [])
        )
        return (
            f"ID: {from_gid(p['id'])}\n"
            f"Title: {p['title']}\n"
            f"Handle: {p['handle']}\n"
            f"Status: {p['status']}\n"
            f"Variants:\n{variants}"
        )

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
        data = client.execute(GET_PRODUCT_BY_ID, {"id": to_gid("Product", product_id)})
        product = data.get("product", {}) or {}
        old_title = product.get("title", "")
        old_handle = product.get("handle", "")

        slugified = slugify_shopify_handle(new_title)
        if change_handle:
            target_handle = slugified
            if target_handle == old_handle:
                handle_block = (
                    f"  Handle     : UNCHANGED (new slug matches existing: {old_handle})"
                )
            else:
                handle_block = (
                    f"  Old handle : {old_handle}\n"
                    f"  New handle : {target_handle}"
                )
        else:
            target_handle = old_handle
            handle_block = f"  Handle     : UNCHANGED (preserved; change_handle=False)"

        validation = format_validation_diff(old_title, new_title)

        preview = (
            f"PREVIEW — Product title update\n"
            f"  Product ID : {product_id}\n"
            f"  Old title  : {old_title}\n"
            f"  New title  : {new_title}\n"
            f"{handle_block}\n\n"
            f"Naming validation:\n{validation}"
        )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        inp = {"id": to_gid("Product", product_id), "title": new_title, "handle": target_handle}

        result = client.execute(UPDATE_PRODUCT, {"input": inp})
        err = format_user_errors(result, "productUpdate")
        if err:
            return err

        log_write(
            "update_product_title",
            f"id={product_id} | '{old_title}' → '{new_title}' | handle '{old_handle}' → '{target_handle}'",
        )
        return f"Done. {preview}"

    @server.tool()
    def update_product_description(
        product_id: str,
        new_description: str,
        confirm: bool = False,
    ) -> str:
        """
        Update a product's body_html description. Returns a preview unless confirm=True.
        """
        data = client.execute(GET_PRODUCT_BY_ID, {"id": to_gid("Product", product_id)})
        product = data.get("product", {})
        old_desc = product.get("bodyHtml", "")

        preview = (
            f"PREVIEW — Product description update\n"
            f"  Product ID   : {product_id}\n"
            f"  Old (excerpt): {old_desc[:120]}{'...' if len(old_desc) > 120 else ''}\n"
            f"  New (excerpt): {new_description[:120]}{'...' if len(new_description) > 120 else ''}"
        )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        result = client.execute(UPDATE_PRODUCT, {
            "input": {"id": to_gid("Product", product_id), "descriptionHtml": new_description}
        })
        err = format_user_errors(result, "productUpdate")
        if err:
            return err

        log_write("update_product_description", f"id={product_id}")
        return f"Done. {preview}"

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

        data = client.execute(GET_PRODUCT_SEO_BY_ID, {"id": to_gid("Product", product_id)})
        product = data.get("product")
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

        old_title_line = old_title if old_title else "(empty)"
        old_desc_line = old_desc if old_desc else "(empty)"
        new_title_line = (
            f"{new_seo_title} ({len(new_seo_title)} chars)"
            if new_seo_title else "(unchanged)"
        )
        new_desc_line = (
            f"{new_seo_description} ({len(new_seo_description)} chars)"
            if new_seo_description else "(unchanged)"
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

        if not confirm:
            return (
                f"PREVIEW — Product SEO update\n{body}"
                f"\n\nTo apply, call again with confirm=True."
            )

        seo_input = {}
        if new_seo_title:
            seo_input["title"] = new_seo_title
        if new_seo_description:
            seo_input["description"] = new_seo_description

        result = client.execute(UPDATE_PRODUCT, {
            "input": {"id": to_gid("Product", product_id), "seo": seo_input}
        })
        err = format_user_errors(result, "productUpdate")
        if err:
            return err

        changed = []
        if new_seo_title:
            changed.append(
                f"title: {len(old_title)} chars → {len(new_seo_title)} chars"
            )
        if new_seo_description:
            changed.append(
                f"description: {len(old_desc)} chars → {len(new_seo_description)} chars"
            )
        log_write("update_product_seo", f"id={product_id} | " + " | ".join(changed))
        return f"CONFIRMED — Product SEO updated\n{body}"

    @server.tool()
    def get_products_by_collection(collection_handle: str) -> str:
        """List all products in a collection by collection handle."""
        data = client.execute(GET_PRODUCTS_BY_COLLECTION, {
            "handle": collection_handle,
            "first": 250,
        })
        col = data.get("collectionByHandle")
        if not col:
            return f"No collection found with handle '{collection_handle}'."

        products = col.get("products", {}).get("nodes", [])
        if not products:
            return f"No products in collection '{collection_handle}'."

        lines = [f"Products in '{collection_handle}' ({len(products)} total):\n"]
        for p in products:
            lines.append(f"  [{from_gid(p['id'])}] {p['title']} | handle: {p['handle']} | {p['status']}")
        return "\n".join(lines)

    @server.tool()
    def get_product_description(product_id: str = "", handle: str = "") -> str:
        """Get the raw body_html description for a single product by id or handle."""
        if product_id:
            data = client.execute(GET_PRODUCT_BY_ID, {"id": to_gid("Product", product_id)})
            p = data.get("product")
        elif handle:
            data = client.execute(GET_PRODUCT_BY_HANDLE, {"handle": handle})
            p = data.get("productByHandle")
        else:
            return "Provide either product_id or handle."

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
            data = client.execute(GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS, {
                "handle": collection_handle,
                "first": limit,
            })
            col = data.get("collectionByHandle")
            if not col:
                return f"No collection found with handle '{collection_handle}'."
            products = col.get("products", {}).get("nodes", [])
            header = f"Products in '{collection_handle}' ({len(products)} total):"
        else:
            data = client.execute(GET_PRODUCTS_WITH_DESCRIPTIONS, {"first": limit})
            products = data.get("products", {}).get("nodes", [])
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
        product_type, vendor, seo, and variants.
        """
        if product_id:
            data = client.execute(GET_PRODUCT_FULL_BY_ID, {"id": to_gid("Product", product_id)})
            p = data.get("product")
        elif handle:
            data = client.execute(GET_PRODUCT_FULL_BY_HANDLE, {"handle": handle})
            p = data.get("productByHandle")
        else:
            return "Provide either product_id or handle."

        if not p:
            return "No product found."

        variants = "\n".join(
            f"  • {v['title']} — SKU: {v.get('sku','N/A')} — id: {from_gid(v['id'])}"
            for v in p.get("variants", {}).get("nodes", [])
        )
        tags = ", ".join(p.get("tags") or []) or "(none)"
        seo = p.get("seo") or {}
        seo_title = seo.get("title") or "(none)"
        seo_desc = seo.get("description") or "(none)"

        return (
            f"ID: {from_gid(p['id'])}\n"
            f"Title: {p['title']}\n"
            f"Handle: {p['handle']}\n"
            f"Status: {p['status']}\n"
            f"Product type: {p.get('productType') or '(none)'}\n"
            f"Vendor: {p.get('vendor') or '(none)'}\n"
            f"Tags: {tags}\n"
            f"SEO title: {seo_title}\n"
            f"SEO description: {seo_desc}\n"
            f"Variants:\n{variants}\n"
            f"body_html:\n{p.get('bodyHtml') or ''}"
        )

    @server.tool()
    def get_product_collections(product_id: str) -> str:
        """List every collection this product belongs to — manual and smart."""
        data = client.execute(GET_PRODUCT_COLLECTIONS, {"id": to_gid("Product", product_id)})
        product = data.get("product")
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
        new_tags: list[str] = None,
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

        gid = to_gid("Product", product_id)
        old_tags: list[str] = []
        if mode in ("append", "remove"):
            data = client.execute(GET_PRODUCT_FULL_BY_ID, {"id": gid})
            product = data.get("product")
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

        if not confirm:
            return (
                f"PREVIEW — Product tags update\n{body}"
                f"\n\nTo apply, call again with confirm=True."
            )

        result = client.execute(UPDATE_PRODUCT_TAGS, {
            "input": {"id": gid, "tags": target}
        })
        err = format_user_errors(result, "productUpdate")
        if err:
            return err

        log_write(
            "update_product_tags",
            f"id={product_id} mode={mode} | {len(old_tags)} tags → {len(target)} tags",
        )
        return f"CONFIRMED — Product tags updated\n{body}"

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
            return (
                f"Error: new_status must be one of {', '.join(PRODUCT_STATUS_VALUES)}."
            )

        gid = to_gid("Product", product_id)
        data = client.execute(GET_PRODUCT_BY_ID, {"id": gid})
        product = data.get("product")
        if not product:
            return f"No product found with id {product_id}."
        old_status = product.get("status") or "(unknown)"

        no_op_suffix = "  (no-op — already at target status)" if old_status == new_status else ""
        body = (
            f"  Product ID : {product_id}\n"
            f"  Old status : {old_status}\n"
            f"  New status : {new_status}{no_op_suffix}"
        )

        if not confirm:
            return (
                f"PREVIEW — Product status update\n{body}"
                f"\n\nTo apply, call again with confirm=True."
            )

        result = client.execute(UPDATE_PRODUCT_STATUS, {
            "input": {"id": gid, "status": new_status}
        })
        err = format_user_errors(result, "productUpdate")
        if err:
            return err

        log_write(
            "update_product_status",
            f"id={product_id} | {old_status} → {new_status}",
        )
        return f"CONFIRMED — Product status updated\n{body}"

    @server.tool()
    def update_variant_inventory_policy(
        product_id: str,
        new_policy: str,
        variant_ids: list[str] = None,
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
            return (
                f"Error: new_policy must be one of {', '.join(INVENTORY_POLICY_VALUES)}."
            )

        product_gid = to_gid("Product", product_id)
        data = client.execute(GET_PRODUCT_VARIANTS_POLICY, {"id": product_gid})
        product = data.get("product")
        if not product:
            return f"No product found with id {product_id}."

        variants = (product.get("variants") or {}).get("nodes", []) or []
        title = product.get("title", "")
        at_cap_warning = (
            f"  WARNING: variant read hit the {VARIANTS_PAGE_CAP}-variant page "
            f"cap — additional variants (if any) are not covered by this call."
        ) if len(variants) >= VARIANTS_PAGE_CAP else ""

        targets, unresolved = filter_variant_targets(variant_ids, variants)

        target_lines = "\n".join(
            f"    • {v['title']} — id: {from_gid(v['id'])} — "
            f"{v.get('inventoryPolicy', '(unknown)')} → {new_policy}"
            for v in targets
        ) or "    (none)"
        unresolved_block = (
            "\n  Unresolved variant ids:\n" +
            "\n".join(f"    • {vid}" for vid in unresolved)
        ) if unresolved else ""

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
            return preview + "\n\nTo apply, call again with confirm=True."

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

        variants_input = [
            {"id": v["id"], "inventoryPolicy": new_policy} for v in targets
        ]
        result = client.execute(UPDATE_PRODUCT_VARIANTS_POLICY, {
            "productId": product_gid,
            "variants": variants_input,
        })
        user_errors = extract_user_errors(result, "productVariantsBulkUpdate")
        if user_errors:
            # `field` is a dotted path list on productVariantsBulkUpdate (e.g.
            # ["variants", "0", "inventoryPolicy"]) — unlike simple scalar-field
            # mutations — so format_user_errors' stringify-field logic won't
            # render it readably. Keep the local formatter.
            def _fmt(e):
                field_path = ".".join(str(f) for f in (e.get("field") or []))
                return f"{field_path or '(no field)'}: {e.get('message', '')}"
            msgs = "; ".join(_fmt(e) for e in user_errors)
            return f"Error: {msgs}"

        updated = (result.get("productVariantsBulkUpdate") or {}).get("productVariants") or []
        updated_lines = "\n".join(
            f"    • id: {from_gid(v['id'])} — inventoryPolicy: {v.get('inventoryPolicy')}"
            for v in updated
        ) or "    (none returned)"

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
