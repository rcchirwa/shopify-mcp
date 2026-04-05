"""
Connection test script.
Run after setting up .env to verify Shopify API credentials work.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  python3 test_shopify_mcp.py
"""

import sys
import os

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

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


if __name__ == "__main__":
    test_connection()
    test_products()
    print("\nAll tests passed. MCP server is ready to register with Claude Desktop.")
