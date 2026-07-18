"""`list_product_media` — read-only listing of a product's media."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_client import ShopifyClient
from tools._gid import from_gid
from tools._untrusted import INJECTION_REMINDER, wrap
from tools.media._common import _as_product_gid
from tools.media._constants import _MEDIA_PAGE_CAP
from tools.media._graphql import GET_PRODUCT_MEDIA


def _render_media_list(
    product: dict[str, Any], nodes: list[dict[str, Any]], capped: bool = False
) -> str:
    """Format a product's media list as a string."""
    if not product:
        return "No product found."
    pid = from_gid(product.get("id", ""))
    header = f"Media for product {pid} ({product.get('title', '')}) — {len(nodes)} item(s):"
    if not nodes:
        return header + "\n  (no media)"

    lines = [header]
    for idx, n in enumerate(nodes, start=1):
        preview = ((n.get("preview") or {}).get("image") or {}).get("url") or "(no preview)"
        # Alt text is shopper/third-party-influenced free text — wrap it as
        # untrusted so the model treats it as data, not instructions (Story
        # 10.41 / SEC-04). An empty alt has no shopper content, so it renders
        # as '' with no wrapper (mirrors orders.py's conditional wrapping).
        raw_alt = n.get("alt") or ""
        alt_display = wrap(raw_alt) if raw_alt else ""
        kind = n.get("mediaContentType") or "UNKNOWN"
        status = n.get("status") or "UNKNOWN"
        lines.append(
            f"  {idx}. {kind} {n.get('id', '')}  status={status}  alt={alt_display!r}\n"
            f"     preview: {preview}"
        )
    if capped:
        lines.append(
            f"  WARNING: pagination cap reached ({len(nodes)} media shown) — "
            f"additional media exist but are not listed here."
        )
    # Media list reached here only when nodes exist, so at least one alt field
    # is present — prefix the injection reminder that explains the tag.
    return INJECTION_REMINDER + "\n".join(lines)


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
        first_response, media_nodes, capped = client.paginate(
            GET_PRODUCT_MEDIA,
            {"id": gid},
            connection_path=["product", "media"],
            page_size=_MEDIA_PAGE_CAP,
        )
        product = first_response.get("product")
        if not product:
            return f"No product found with id {product_id}."
        return _render_media_list(product, media_nodes, capped)
