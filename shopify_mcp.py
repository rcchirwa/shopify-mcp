"""
Shopify MCP Server — entry point.

Exposes Shopify Admin API tools to Claude via the Model Context Protocol.
Run via: python shopify_mcp.py

All credentials loaded from .env — never hardcoded here.
"""

from mcp.server.fastmcp import FastMCP

import tools.collections as collections_module
import tools.discounts as discounts_module
import tools.inventory as inventory_module
import tools.media as media_module
import tools.orders as orders_module
import tools.products as products_module
import tools.publications as publications_module
import tools.webhooks as webhooks_module
from shopify_client import ShopifyClient


def create_server() -> FastMCP:
    server = FastMCP("shopify-aon")
    client = ShopifyClient()

    products_module.register(server, client)
    inventory_module.register(server, client)
    collections_module.register(server, client)
    discounts_module.register(server, client)
    orders_module.register(server, client)
    publications_module.register(server, client)
    webhooks_module.register(server, client)
    media_module.register(server, client)

    return server


def main():
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
