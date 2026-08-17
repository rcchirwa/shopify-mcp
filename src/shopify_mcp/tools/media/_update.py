"""`update_product_media` — update alt text on existing media."""

from mcp.server.fastmcp import FastMCP

from shopify_mcp.client import ShopifyClient
from shopify_mcp.tools._log import log_write
from shopify_mcp.tools._response import extract_user_errors, with_confirm_hint
from shopify_mcp.tools._untrusted import with_reminder, wrap
from shopify_mcp.tools.media._common import _as_product_gid, _fmt_media_user_errors
from shopify_mcp.tools.media._constants import _MEDIA_PAGE_CAP
from shopify_mcp.tools.media._graphql import GET_PRODUCT_MEDIA, PRODUCT_UPDATE_MEDIA


def register(server: FastMCP, client: ShopifyClient) -> None:
    @server.tool()
    def update_product_media(
        product_id: str,
        media_id: str,
        alt: str,
        confirm: bool = False,
    ) -> str:
        """
        Update the alt text on an existing piece of product media.

        Scope note: productUpdateMedia only updates alt text and a few other
        attributes — it does NOT swap the image file. To swap an image, use
        delete_product_media + upload_product_image. Returns a preview
        unless confirm=True.
        """
        if not media_id:
            return "Error: provide media_id."
        gid = _as_product_gid(product_id)
        if not gid:
            return "Error: provide product_id."

        first_response, nodes, _ = client.paginate(
            GET_PRODUCT_MEDIA,
            {"id": gid},
            connection_path=["product", "media"],
            page_size=_MEDIA_PAGE_CAP,
        )
        product = first_response.get("product")
        if not product:
            return f"No product found with id {product_id}."
        target = next((n for n in nodes if n.get("id") == media_id), None)
        if not target:
            return f"Error: media {media_id} is not attached to product {product_id}."
        old_alt = target.get("alt") or ""
        # `old_alt` is stored store content read back over the wire — the same
        # `alt` field list_product_media has fenced since SEC-04 — so it is
        # wrapped here too (Story 10.63 / SEC-04-descriptions; the 10.41
        # deferral called this echo "mostly caller-supplied input", which is
        # true of the New half only). `alt` is the caller's own just-submitted
        # value and stays raw. The no-op compare uses the RAW values, so
        # wrapping can never make an unchanged alt look changed.
        no_op_suffix = "  (no-op — alt unchanged)" if old_alt == alt else ""
        old_alt_display = wrap(old_alt) if old_alt else ""
        body = (
            f"  Product ID : {product_id}\n"
            f"  Media ID   : {media_id}\n"
            f"  Old alt    : {old_alt_display!r}\n"
            f"  New alt    : {alt!r}{no_op_suffix}"
        )

        if not confirm:
            return with_confirm_hint(with_reminder(f"PREVIEW — Update product media alt\n{body}"))

        result = client.execute(
            PRODUCT_UPDATE_MEDIA,
            {
                "productId": gid,
                "media": [{"id": media_id, "alt": alt}],
            },
        )
        errors = extract_user_errors(result, "productUpdateMedia", error_key="mediaUserErrors")
        if errors:
            return _fmt_media_user_errors(errors, "update")

        log_write(
            "update_product_media",
            f"product={product_id} media={media_id} alt_len {len(old_alt)}->{len(alt)}",
        )
        return with_reminder(f"CONFIRMED — Update product media alt\n{body}")
