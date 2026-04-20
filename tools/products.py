"""
Product tools — read and write Shopify products.

Write operations require confirm=True and log to aon_mcp_log.txt.
"""

import re

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient, to_gid, from_gid
from validators.naming import format_validation_result
from tools._log import log_write

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

GET_PRODUCT_SEO_BY_ID = """
query GetProductSeoById($id: ID!) {
  product(id: $id) {
    id
    title
    seo { title description }
  }
}
"""


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

        validation = format_validation_result(new_title)

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
        user_errors = result.get("productUpdate", {}).get("userErrors", [])
        if user_errors:
            msgs = "; ".join(f"{e['field']}: {e['message']}" for e in user_errors)
            return f"Error: {msgs}"

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
        user_errors = result.get("productUpdate", {}).get("userErrors", [])
        if user_errors:
            msgs = "; ".join(f"{e['field']}: {e['message']}" for e in user_errors)
            return f"Error: {msgs}"

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
        user_errors = result.get("productUpdate", {}).get("userErrors", [])
        if user_errors:
            msgs = "; ".join(f"{e['field']}: {e['message']}" for e in user_errors)
            return f"Error: {msgs}"

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
