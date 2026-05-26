"""
Shopify MCP Server — entry point.

Exposes Shopify Admin API tools to Claude via the Model Context Protocol.
Run via: python shopify_mcp.py

All credentials loaded from .env — never hardcoded here.
"""

import logging

from mcp.server.fastmcp import FastMCP

import tools.catalog_hygiene as catalog_hygiene_module
import tools.collections as collections_module
import tools.discounts as discounts_module
import tools.inventory as inventory_module
import tools.media as media_module
import tools.orders as orders_module
import tools.products as products_module
import tools.publications as publications_module
import tools.webhooks as webhooks_module
from logging_config import configure_logging
from settings import Settings
from shopify_client import ShopifyClient

logger = logging.getLogger(__name__)


def create_server() -> FastMCP:
    configure_logging(Settings())  # type: ignore[call-arg]
    server = FastMCP("shopify-aon")
    client = ShopifyClient()
    logger.info("shopify-aon MCP server initialized")

    products_module.register(server, client)
    inventory_module.register(server, client)
    collections_module.register(server, client)
    discounts_module.register(server, client)
    orders_module.register(server, client)
    publications_module.register(server, client)
    webhooks_module.register(server, client)
    media_module.register(server, client)
    catalog_hygiene_module.register(server, client)

    return server


def main() -> None:
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
