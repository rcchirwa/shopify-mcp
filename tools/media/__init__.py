"""
Media tools — list, upload, reorder, update, and delete product media.

All write operations require confirm=True and log to aon_mcp_log.txt.
Package facade; see submodules for implementations.
"""

from mcp.server.fastmcp import FastMCP

from shopify_client import ShopifyClient
from tools.media import _delete, _list, _reorder, _update, _upload


def register(server: FastMCP, client: ShopifyClient):
    """Register all media tools on the server."""
    _list.register(server, client)
    _upload.register(server, client)
    _reorder.register(server, client)
    _update.register(server, client)
    _delete.register(server, client)
