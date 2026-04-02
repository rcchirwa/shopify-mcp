"""
Product tools — read and write Shopify products.

Write operations require confirm=True and log to aon_mcp_log.txt.
"""

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient
from validators.naming import format_validation_result
from tools._log import log_write


def register(server: FastMCP, client: ShopifyClient):

    @server.tool()
    def get_products() -> str:
        """List all products with id, title, handle, status, and variants."""
        data = client.get("/products.json", {"limit": 250, "fields": "id,title,handle,status,variants"})
        products = data.get("products", [])
        if not products:
            return "No products found."
        lines = []
        for p in products:
            variants = ", ".join(
                f"{v['title']} (id:{v['id']})" for v in p.get("variants", [])
            )
            lines.append(
                f"[{p['id']}] {p['title']} | handle: {p['handle']} | status: {p['status']}\n"
                f"  Variants: {variants}"
            )
        return "\n\n".join(lines)

    @server.tool()
    def get_product(product_id: str = "", handle: str = "") -> str:
        """Get a single product by id or handle."""
        if product_id:
            data = client.get(f"/products/{product_id}.json")
        elif handle:
            data = client.get("/products.json", {"handle": handle})
            products = data.get("products", [])
            if not products:
                return f"No product found with handle '{handle}'."
            data = {"product": products[0]}
        else:
            return "Provide either product_id or handle."

        p = data.get("product", {})
        variants = "\n".join(
            f"  • {v['title']} — SKU: {v.get('sku','N/A')} — id: {v['id']}"
            for v in p.get("variants", [])
        )
        return (
            f"ID: {p['id']}\n"
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
        Never changes the URL handle unless change_handle=True.
        """
        data = client.get(f"/products/{product_id}.json")
        product = data.get("product", {})
        old_title = product.get("title", "")

        validation = format_validation_result(new_title)

        preview = (
            f"PREVIEW — Product title update\n"
            f"  Product ID : {product_id}\n"
            f"  Old title  : {old_title}\n"
            f"  New title  : {new_title}\n"
            f"  Handle     : {'UNCHANGED' if not change_handle else 'WILL BE UPDATED'}\n\n"
            f"Naming validation:\n{validation}"
        )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        payload = {"product": {"id": product_id, "title": new_title}}
        if not change_handle:
            payload["product"]["handle"] = product.get("handle")

        client.put(f"/products/{product_id}.json", payload)
        log_write("update_product_title", f"id={product_id} | '{old_title}' → '{new_title}'")
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
        data = client.get(f"/products/{product_id}.json")
        product = data.get("product", {})
        old_desc = product.get("body_html", "")

        preview = (
            f"PREVIEW — Product description update\n"
            f"  Product ID   : {product_id}\n"
            f"  Old (excerpt): {old_desc[:120]}{'...' if len(old_desc) > 120 else ''}\n"
            f"  New (excerpt): {new_description[:120]}{'...' if len(new_description) > 120 else ''}"
        )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        client.put(
            f"/products/{product_id}.json",
            {"product": {"id": product_id, "body_html": new_description}},
        )
        log_write("update_product_description", f"id={product_id}")
        return f"Done. {preview}"

    @server.tool()
    def get_products_by_collection(collection_handle: str) -> str:
        """List all products in a collection by collection handle."""
        # Resolve handle to collection id
        custom = client.get("/custom_collections.json", {"handle": collection_handle})
        collections = custom.get("custom_collections", [])
        if not collections:
            smart = client.get("/smart_collections.json", {"handle": collection_handle})
            collections = smart.get("smart_collections", [])
        if not collections:
            return f"No collection found with handle '{collection_handle}'."

        collection_id = collections[0]["id"]
        data = client.get(
            "/products.json",
            {"collection_id": collection_id, "limit": 250, "fields": "id,title,handle,status"},
        )
        products = data.get("products", [])
        if not products:
            return f"No products in collection '{collection_handle}'."

        lines = [f"Products in '{collection_handle}' ({len(products)} total):\n"]
        for p in products:
            lines.append(f"  [{p['id']}] {p['title']} | handle: {p['handle']} | {p['status']}")
        return "\n".join(lines)
