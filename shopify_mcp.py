"""
Shopify MCP Server — entry point.

Exposes Shopify Admin API tools to Claude via the Model Context Protocol.
Run via: python shopify_mcp.py

All credentials loaded from .env — never hardcoded here.
"""

import sys
from mcp.server import Server
from mcp.server.stdio import stdio_server
from shopify_client import ShopifyClient
import tools.products as products_module
import tools.inventory as inventory_module
import tools.collections as collections_module
import tools.discounts as discounts_module
import tools.orders as orders_module


def create_server() -> Server:
    server = Server("shopify-aon")
    client = ShopifyClient()

    products_module.register(server, client)
    inventory_module.register(server, client)
    collections_module.register(server, client)
    discounts_module.register(server, client)
    orders_module.register(server, client)

    return server


def main():
    server = create_server()
    import asyncio
    asyncio.run(stdio_server(server))


if __name__ == "__main__":
    main()
