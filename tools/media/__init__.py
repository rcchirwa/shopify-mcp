"""
Media tools — list, upload, reorder, update, and delete product media.

All write operations require confirm=True and log to aon_mcp_log.txt.
Package facade; see submodules for implementations.
"""

from mcp.server.fastmcp import FastMCP

from shopify_client import ShopifyClient
from tools.media import _delete, _list, _reorder, _update, _upload

# Re-exports so `from tools.media import X` still works for existing callers/tests.
from tools.media._common import _as_product_gid  # noqa: F401
from tools.media._graphql import (  # noqa: F401
    GET_MEDIA_STATUS,
    GET_PRODUCT_MEDIA,
    PRODUCT_CREATE_MEDIA,
    PRODUCT_DELETE_MEDIA,
    PRODUCT_REORDER_MEDIA,
    PRODUCT_UPDATE_MEDIA,
    STAGED_UPLOADS_CREATE,
)
from tools.media._list import _render_media_list  # noqa: F401
from tools.media._upload import (  # noqa: F401
    _download_image,
    _format_bytes,
    _upload_bytes_to_target,
)


def register(server: FastMCP, client: ShopifyClient):
    """Register all media tools on the server."""
    _list.register(server, client)
    _upload.register(server, client)
    _reorder.register(server, client)
    _update.register(server, client)
    _delete.register(server, client)
