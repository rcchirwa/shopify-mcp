"""
Product tools — read and write Shopify products.

Write operations require confirm=True and log to aon_mcp_log.txt.
"""

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient, to_gid, from_gid
from validators.naming import format_validation_result
from tools._log import log_write

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
        Never changes the URL handle unless change_handle=True.
        """
        data = client.execute(GET_PRODUCT_BY_ID, {"id": to_gid("Product", product_id)})
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

        inp = {"id": to_gid("Product", product_id), "title": new_title}
        if not change_handle:
            inp["handle"] = product.get("handle")

        result = client.execute(UPDATE_PRODUCT, {"input": inp})
        user_errors = result.get("productUpdate", {}).get("userErrors", [])
        if user_errors:
            msgs = "; ".join(f"{e['field']}: {e['message']}" for e in user_errors)
            return f"Error: {msgs}"

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
