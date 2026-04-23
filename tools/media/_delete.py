"""`delete_product_media` — remove media from a product by id."""

from mcp.server.fastmcp import FastMCP

from shopify_client import ShopifyClient, extract_user_errors, with_confirm_hint
from tools._log import log_write
from tools.media._common import _as_product_gid, _fmt_media_user_errors
from tools.media._graphql import GET_PRODUCT_MEDIA, PRODUCT_DELETE_MEDIA


def register(server: FastMCP, client: ShopifyClient):
    @server.tool()
    def delete_product_media(
        product_id: str,
        media_ids: list[str] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Remove media from a product by media ID. Accepts one or more IDs.
        Returns a preview unless confirm=True.
        """
        if not media_ids:
            return "Error: media_ids must be a non-empty list."
        gid = _as_product_gid(product_id)
        if not gid:
            return "Error: provide product_id."

        data = client.execute(GET_PRODUCT_MEDIA, {"id": gid})
        product = data.get("product")
        if not product:
            return f"No product found with id {product_id}."
        nodes = (product.get("media") or {}).get("nodes", []) or []
        current_index = {n.get("id"): n for n in nodes}

        # Match caller-supplied ids to what's actually attached. Dedup while
        # preserving order to keep the preview stable.
        seen = set()
        ordered_ids = []
        for mid in media_ids:
            if mid not in seen:
                seen.add(mid)
                ordered_ids.append(mid)
        matched = [mid for mid in ordered_ids if mid in current_index]
        unmatched = [mid for mid in ordered_ids if mid not in current_index]

        def _fmt_line(mid):
            n = current_index.get(mid) or {}
            preview = ((n.get("preview") or {}).get("image") or {}).get("url") or "(no preview)"
            return f"    • {mid}  alt={(n.get('alt') or '')!r}\n      preview: {preview}"

        matched_block = "\n".join(_fmt_line(mid) for mid in matched) or "    (none)"
        unmatched_block = (
            (
                "\n  Not attached (will be skipped by Shopify):\n"
                + "\n".join(f"    • {mid}" for mid in unmatched)
            )
            if unmatched
            else ""
        )

        preview = (
            f"PREVIEW — Delete product media\n"
            f"  Product ID : {product_id}\n"
            f"  To delete ({len(matched)}):\n{matched_block}"
            f"{unmatched_block}"
        )

        if not confirm:
            return with_confirm_hint(preview)

        if not matched:
            log_write(
                "delete_product_media",
                f"product={product_id} deleted=0 unmatched={len(unmatched)}",
            )
            return (
                f"CONFIRMED — Delete product media (no-op)\n"
                f"  Product ID : {product_id}\n"
                f"  Nothing to delete — every requested id was unattached."
                f"{unmatched_block}"
            )

        result = client.execute(
            PRODUCT_DELETE_MEDIA,
            {
                "productId": gid,
                "mediaIds": matched,
            },
        )
        payload = result.get("productDeleteMedia", {}) or {}
        errors = extract_user_errors(result, "productDeleteMedia", error_key="mediaUserErrors")
        if errors:
            return _fmt_media_user_errors(errors, "delete")
        deleted = payload.get("deletedMediaIds") or []

        log_write(
            "delete_product_media",
            f"product={product_id} deleted={len(deleted)} unmatched={len(unmatched)} ids={deleted}",
        )
        deleted_block = "\n".join(f"    • {mid}" for mid in deleted) or "    (none)"
        return (
            f"CONFIRMED — Delete product media\n"
            f"  Product ID : {product_id}\n"
            f"  Deleted ({len(deleted)}):\n{deleted_block}"
            f"{unmatched_block}"
        )
