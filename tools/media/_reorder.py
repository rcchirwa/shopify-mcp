"""`reorder_product_media` — change display order of a product's media."""

from mcp.server.fastmcp import FastMCP

from shopify_client import (
    JOB_POLL_TIMEOUT_S,
    ShopifyClient,
    from_gid,
    poll_job,
    with_confirm_hint,
)
from tools._log import log_write
from tools.media._common import (
    _as_product_gid,
    _extract_media_user_errors,
    _fmt_media_user_errors,
)
from tools.media._graphql import GET_PRODUCT_MEDIA, PRODUCT_REORDER_MEDIA


def register(server: FastMCP, client: ShopifyClient):
    @server.tool()
    def reorder_product_media(
        product_id: str,
        moves: list[dict] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Change the display order of media on a product. moves is a list of
        {"id": "gid://shopify/MediaImage/...", "newPosition": 1} items,
        where newPosition is 1-indexed (1 = featured). The tool converts to
        Shopify's 0-indexed string form internally.

        Returns a preview unless confirm=True. Polls the returned job.
        """
        if not moves:
            return "Error: moves must be a non-empty list of {id, newPosition} items."
        gid = _as_product_gid(product_id)
        if not gid:
            return "Error: provide product_id."

        # Normalize + validate inputs before any network call.
        parsed_moves = []
        for m in moves:
            mid = m.get("id") if isinstance(m, dict) else None
            pos = m.get("newPosition") if isinstance(m, dict) else None
            if not mid or not isinstance(pos, int) or pos < 1:
                return f"Error: each move needs id (string) and newPosition (int >= 1). Got: {m!r}"
            parsed_moves.append({"id": mid, "newPosition": pos})

        # Preview: show current order vs proposed, reject unknown ids.
        data = client.execute(GET_PRODUCT_MEDIA, {"id": gid})
        product = data.get("product")
        if not product:
            return f"No product found with id {product_id}."
        current_nodes = (product.get("media") or {}).get("nodes", []) or []
        current_ids = [n.get("id") for n in current_nodes]
        unknown = [m["id"] for m in parsed_moves if m["id"] not in current_ids]
        if unknown:
            return "Error: these media ids are not attached to the product: " + ", ".join(unknown)

        current_lines = (
            "\n".join(
                f"    {i + 1}. {n.get('id')}  alt={(n.get('alt') or '')!r}"
                for i, n in enumerate(current_nodes)
            )
            or "    (none)"
        )
        moves_lines = "\n".join(
            f"    • {m['id']} → position {m['newPosition']}" for m in parsed_moves
        )
        body = (
            f"  Product ID : {product_id}\n"
            f"  Current order ({len(current_nodes)}):\n{current_lines}\n"
            f"  Moves ({len(parsed_moves)}):\n{moves_lines}"
        )

        if not confirm:
            return with_confirm_hint(f"PREVIEW — Reorder product media\n{body}")

        # Shopify's MoveInput.newPosition is 0-indexed and serialized as a
        # string. Convert at the boundary so the caller sees 1-indexed ints
        # everywhere.
        api_moves = [
            {"id": m["id"], "newPosition": str(m["newPosition"] - 1)} for m in parsed_moves
        ]
        result = client.execute(
            PRODUCT_REORDER_MEDIA,
            {
                "id": gid,
                "moves": api_moves,
            },
        )
        payload = result.get("productReorderMedia", {}) or {}
        media_errors = _extract_media_user_errors(result, "productReorderMedia")
        if media_errors:
            return _fmt_media_user_errors(media_errors, "reorder")

        job = payload.get("job") or {}
        job_id = job.get("id")
        initial_done = bool(job.get("done"))
        poll_result = None
        if job_id and not initial_done:
            poll_result = poll_job(client, job_id, timeout_s=JOB_POLL_TIMEOUT_S)

        log_write(
            "reorder_product_media",
            f"product={product_id} moves={len(parsed_moves)} "
            f"job={job_id or '(none)'} "
            f"done={(poll_result['done'] if poll_result else initial_done)}",
        )

        job_line = ""
        if job_id:
            numeric = from_gid(job_id)
            if poll_result is None:
                job_line = f"\n  Job        : {numeric} (done=True)"
            elif poll_result["done"]:
                job_line = (
                    f"\n  Job        : {numeric} (done=True after {poll_result['elapsed_s']:.1f}s)"
                )
            elif poll_result["timed_out"]:
                job_line = (
                    f"\n  Job        : {numeric} (still running after "
                    f"{JOB_POLL_TIMEOUT_S}s timeout — verify via "
                    f"list_product_media)"
                )
        return f"CONFIRMED — Reorder product media\n{body}{job_line}"
