"""
Connection test script.
Run after setting up .env to verify Shopify API credentials work.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  python3 test_shopify_mcp.py
"""

import sys

from shopify_client import ShopifyClient, from_gid

SHOP_QUERY = """
query {
  shop {
    name
    myshopifyDomain
    plan { displayName }
    currencyCode
  }
}
"""

GET_PRODUCTS = """
query GetProducts($first: Int!) {
  products(first: $first) {
    nodes {
      id
      title
      status
    }
  }
}
"""


def test_connection():
    print("Testing Shopify API connection...")
    try:
        client = ShopifyClient()
        data = client.execute(SHOP_QUERY)
        shop = data.get("shop", {})
        print(f"  Connected to: {shop.get('name')} ({shop.get('myshopifyDomain')})")
        print(f"  Plan: {shop.get('plan', {}).get('displayName')}")
        print(f"  Currency: {shop.get('currencyCode')}")
        print("\nConnection test PASSED.")
    except Exception as e:
        print(f"\nConnection test FAILED: {e}")
        sys.exit(1)


def test_products():
    print("\nFetching first 3 products...")
    try:
        client = ShopifyClient()
        data = client.execute(GET_PRODUCTS, {"first": 3})
        products = data.get("products", {}).get("nodes", [])
        for p in products:
            print(f"  [{from_gid(p['id'])}] {p['title']} — {p['status']}")
        print(f"Product fetch test PASSED ({len(products)} products returned).")
    except Exception as e:
        print(f"Product fetch test FAILED: {e}")
        sys.exit(1)


GET_FIRST_PRODUCT = """
query {
  products(first: 1) {
    nodes { id handle }
  }
}
"""

GET_PRODUCT_DESC_BY_ID = """
query($id: ID!) {
  product(id: $id) { id title handle bodyHtml }
}
"""

GET_PRODUCT_DESC_BY_HANDLE = """
query($handle: String!) {
  productByHandle(handle: $handle) { id title handle bodyHtml }
}
"""

GET_PRODUCTS_WITH_DESC = """
query($first: Int!) {
  products(first: $first) {
    nodes { id title handle status bodyHtml }
  }
}
"""

GET_PRODUCT_FULL = """
query($id: ID!) {
  product(id: $id) {
    id title handle status bodyHtml
    tags productType vendor
    seo { title description }
    variants(first: 50) { nodes { id title sku } }
  }
}
"""


def _get_first_product_id_and_handle(client):
    data = client.execute(GET_FIRST_PRODUCT)
    nodes = data.get("products", {}).get("nodes", [])
    if not nodes:
        return None, None
    return nodes[0]["id"], nodes[0]["handle"]


def test_get_product_description():
    print("\nTesting get_product_description queries...")
    try:
        client = ShopifyClient()
        gid, handle = _get_first_product_id_and_handle(client)
        if not gid:
            print("  No products in store — skipping.")
            return

        by_id = client.execute(GET_PRODUCT_DESC_BY_ID, {"id": gid})
        p1 = by_id.get("product")
        assert p1 and "bodyHtml" in p1, "bodyHtml missing from by-id result"

        by_handle = client.execute(GET_PRODUCT_DESC_BY_HANDLE, {"handle": handle})
        p2 = by_handle.get("productByHandle")
        assert p2 and "bodyHtml" in p2, "bodyHtml missing from by-handle result"

        print(f"  by id:     [{from_gid(p1['id'])}] {p1['title']} — body_html present")
        print(f"  by handle: [{from_gid(p2['id'])}] {p2['title']} — body_html present")
        print("get_product_description test PASSED.")
    except Exception as e:
        print(f"get_product_description test FAILED: {e}")
        sys.exit(1)


def test_get_products_with_descriptions():
    print("\nTesting get_products_with_descriptions query...")
    try:
        client = ShopifyClient()
        data = client.execute(GET_PRODUCTS_WITH_DESC, {"first": 3})
        products = data.get("products", {}).get("nodes", [])
        for p in products:
            assert "bodyHtml" in p, f"bodyHtml missing for {p.get('handle')}"
            print(
                f"  [{from_gid(p['id'])}] {p['title']} — body_html len: {len(p.get('bodyHtml') or '')}"
            )
        print(f"get_products_with_descriptions test PASSED ({len(products)} products).")
    except Exception as e:
        print(f"get_products_with_descriptions test FAILED: {e}")
        sys.exit(1)


def test_get_product_full():
    print("\nTesting get_product_full query...")
    try:
        client = ShopifyClient()
        gid, _ = _get_first_product_id_and_handle(client)
        if not gid:
            print("  No products in store — skipping.")
            return

        data = client.execute(GET_PRODUCT_FULL, {"id": gid})
        p = data.get("product")
        for field in ("bodyHtml", "tags", "productType", "vendor", "seo"):
            assert field in p, f"{field} missing from full product result"

        print(f"  [{from_gid(p['id'])}] {p['title']}")
        print(f"  tags: {p.get('tags')}, type: {p.get('productType')}, vendor: {p.get('vendor')}")
        print("get_product_full test PASSED.")
    except Exception as e:
        print(f"get_product_full test FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    test_connection()
    test_products()
    test_get_product_description()
    test_get_products_with_descriptions()
    test_get_product_full()
    print("\nAll tests passed. MCP server is ready to register with Claude Desktop.")
