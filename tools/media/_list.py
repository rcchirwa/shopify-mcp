"""`list_product_media` — read-only listing of a product's media."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_client import ShopifyClient, from_gid
from tools.media._common import _as_product_gid
from tools.media._constants import _MEDIA_PAGE_CAP
from tools.media._graphql import GET_PRODUCT_MEDIA


def _render_media_list(product: dict[str, Any]) -> str:
    """Format a product's media list as a string. `product` is the GraphQL node."""
    if not product:
        return "No product found."
    media = product.get("media") or {}
    nodes = media.get("nodes", []) or []
    pid = from_gid(product.get("id", ""))
    header = f"Media for product {pid} ({product.get('title', '')}) — {len(nodes)} item(s):"
    if not nodes:
        return header + "\n  (no media)"

    lines = [header]
    for idx, n in enumerate(nodes, start=1):
        preview = ((n.get("preview") or {}).get("image") or {}).get("url") or "(no preview)"
        alt = n.get("alt") or ""
        kind = n.get("mediaContentType") or "UNKNOWN"
        status = n.get("status") or "UNKNOWN"
        lines.append(
            f"  {idx}. {kind} {n.get('id', '')}  status={status}  alt={alt!r}\n"
            f"     preview: {preview}"
        )
    if (media.get("pageInfo") or {}).get("hasNextPage"):
        lines.append(
            f"  WARNING: product has more than {_MEDIA_PAGE_CAP} media items — "
            f"additional media exist but are not listed here."
        )
    return "\n".join(lines)


def register(server: FastMCP, client: ShopifyClient) -> None:
    @server.tool()
    def list_product_media(product_id: str) -> str:
        """
        List all media (images, videos, 3D models) attached to a product.
        Returns IDs, content type, status, alt text, and preview URLs in
        display order. Read-only — no confirm required.
        """
        gid = _as_product_gid(product_id)
        if not gid:
            return "Error: provide product_id."
        data = client.execute(GET_PRODUCT_MEDIA, {"id": gid})
        product = data.get("product")
        if not product:
            return f"No product found with id {product_id}."
        return _render_media_list(product)
