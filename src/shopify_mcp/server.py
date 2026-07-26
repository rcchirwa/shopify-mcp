"""
Shopify MCP Server — entry point.

Exposes Shopify Admin API tools to Claude via the Model Context Protocol.
Run via: python shopify_mcp.py

All credentials loaded from .env — never hardcoded here.
"""

import logging
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import shopify_mcp.tools.catalog_hygiene as catalog_hygiene_module
import shopify_mcp.tools.collections as collections_module
import shopify_mcp.tools.discounts as discounts_module
import shopify_mcp.tools.inventory as inventory_module
import shopify_mcp.tools.media as media_module
import shopify_mcp.tools.orders as orders_module
import shopify_mcp.tools.products as products_module
import shopify_mcp.tools.publications as publications_module
import shopify_mcp.tools.webhooks as webhooks_module
from shopify_mcp.client import ShopifyClient
from shopify_mcp.logging_config import configure_logging
from shopify_mcp.settings import Settings

logger = logging.getLogger(__name__)

# .env sits at the repo root, two directories above this module since the src/
# move (Story 10.47 / FS-3): <repo root>/src/shopify_mcp/server.py. Pinned to a
# fixed path rather than discovered, because Claude Desktop launches this
# process with CWD=/ — a CWD-relative lookup finds nothing and the server boots
# with no credentials.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def create_server() -> FastMCP:
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
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
