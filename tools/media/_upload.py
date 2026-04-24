"""`upload_product_image` — staged-upload pipeline and its helpers.

Upload flow is staged: stagedUploadsCreate -> HTTP PUT to signed target ->
productCreateMedia attaches the resourceUrl. Per the Shopify 2026-01 spec
(see https://shopify.dev/docs/apps/build/online-store/product-media),
image uploads use HTTP PUT with the returned `parameters` applied as headers,
not multipart form fields — that shape is reserved for video / 3D model uploads,
which are out of scope for v1.
"""

import mimetypes
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from mcp.server.fastmcp import FastMCP

from shopify_client import (
    JOB_POLL_TIMEOUT_S,
    ShopifyClient,
    extract_user_errors,
    from_gid,
    poll_job,
    with_confirm_hint,
)
from tools._log import log_write
from tools._url_safety import _reject_if_private_host
from tools.media._common import (
    _as_product_gid,
    _extract_media_user_errors,
    _fmt_media_user_errors,
)
from tools.media._constants import (
    _IMAGE_DOWNLOAD_TIMEOUT_S,
    _MAX_IMAGE_BYTES,
    _MEDIA_PROCESSING_POLL_INTERVAL_S,
    _MEDIA_PROCESSING_POLL_TIMEOUT_S,
)
from tools.media._graphql import (
    GET_MEDIA_STATUS,
    GET_PRODUCT_MEDIA,
    PRODUCT_CREATE_MEDIA,
    PRODUCT_REORDER_MEDIA,
    STAGED_UPLOADS_CREATE,
)


def _format_bytes(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "? bytes"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _filename_from_url(url: str) -> str:
    """Extract a filename from a URL path; fall back to a generic name."""
    path = urlparse(url).path
    name = Path(path).name
    return name or "upload.bin"


def _download_image(url: str) -> tuple[bytes, str, str]:
    """Download an image URL and return (bytes, filename, mime_type).

    Raises RuntimeError with a human-readable detail on any failure. Caller is
    expected to wrap the call and label it as `stage=download` on error.
    """
    _reject_if_private_host(url)
    try:
        resp = requests.get(url, stream=True, timeout=_IMAGE_DOWNLOAD_TIMEOUT_S)
    except requests.RequestException as e:
        raise RuntimeError(f"request failed: {e}") from e

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} from source URL")

    # Refuse huge files before we pull all bytes into memory. `Content-Length`
    # is advisory — the streaming loop below enforces the cap again.
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit() and int(cl) > _MAX_IMAGE_BYTES:
        raise RuntimeError(
            f"source is {_format_bytes(cl)} — exceeds Shopify's "
            f"{_format_bytes(_MAX_IMAGE_BYTES)} image cap"
        )

    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > _MAX_IMAGE_BYTES:
            raise RuntimeError(f"source exceeded {_format_bytes(_MAX_IMAGE_BYTES)} during download")

    filename = _filename_from_url(url)
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not content_type:
        guessed, _ = mimetypes.guess_type(filename)
        content_type = (guessed or "").lower()
    if not content_type.startswith("image/"):
        raise RuntimeError(
            f"unsupported MIME type: {content_type or '(unknown)'} — v1 accepts images only"
        )
    return bytes(buf), filename, content_type


def _upload_bytes_to_target(target: dict, image_bytes: bytes) -> None:
    """PUT image bytes to the staged target URL, with parameters as headers.

    Raises RuntimeError on non-2xx. Caller labels the failure stage.
    """
    url = target.get("url")
    if not url:
        raise RuntimeError("staged target missing 'url'")
    params = target.get("parameters") or []
    headers = {p["name"]: p["value"] for p in params if p.get("name")}
    try:
        resp = requests.put(url, data=image_bytes, headers=headers, timeout=60)
    except requests.RequestException as e:
        raise RuntimeError(f"PUT to staged target failed: {e}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"staged target returned HTTP {resp.status_code}: {resp.text[:300]}")


def _poll_media_ready(client: ShopifyClient, product_gid: str, media_id: str) -> dict:
    """Poll the media node until it leaves PROCESSING or the budget expires.

    Reads just the target node via `node(id)` rather than the whole product
    media connection — constant-time regardless of how many media the
    product has. `product_gid` is retained in the signature for future
    provenance/logging but is no longer needed for the query itself.

    Returns dict: { status: str, preview_url: str or None, timed_out: bool,
                    elapsed_s: float }

    Transient read failures during the loop are logged to stderr but don't
    abort — the whole point of polling is to keep trying until the budget is
    up. Logging makes a pathological "loop until timeout with no signal"
    observable instead of silent.
    """
    del product_gid  # reserved for future provenance/logging
    start = time.monotonic()
    last = {"status": "PROCESSING", "preview_url": None}
    while True:
        try:
            data = client.execute(GET_MEDIA_STATUS, {"id": media_id})
            node = data.get("node") or {}
            if node:
                last["status"] = node.get("status") or "PROCESSING"
                last["preview_url"] = ((node.get("preview") or {}).get("image") or {}).get("url")
        except Exception as e:
            print(
                f"[media] poll warning for {media_id}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )

        elapsed = time.monotonic() - start
        if last["status"] in ("READY", "FAILED"):
            return {**last, "timed_out": False, "elapsed_s": elapsed}
        if elapsed + _MEDIA_PROCESSING_POLL_INTERVAL_S > _MEDIA_PROCESSING_POLL_TIMEOUT_S:
            return {**last, "timed_out": True, "elapsed_s": elapsed}
        time.sleep(_MEDIA_PROCESSING_POLL_INTERVAL_S)


def _stage_upload(
    client: ShopifyClient, filename: str, mime_type: str, size: int
) -> tuple[dict[str, Any] | None, str | None]:
    """Create a stagedUploadsCreate target for a subsequent PUT.

    Returns (target_dict, None) on success, (None, error_str) on failure.
    error_str is already prefixed with `Error at stage=stage_upload:` so the
    caller can return it directly.
    """
    try:
        staged = client.execute(
            STAGED_UPLOADS_CREATE,
            {
                "input": [
                    {
                        "resource": "IMAGE",
                        "filename": filename,
                        "mimeType": mime_type,
                        "httpMethod": "PUT",
                        "fileSize": str(size),
                    }
                ],
            },
        )
    except Exception as e:
        return None, f"Error at stage=stage_upload: {e}"
    staged_errors = extract_user_errors(staged, "stagedUploadsCreate")
    if staged_errors:
        return None, _fmt_media_user_errors(staged_errors, "stage_upload")
    targets = (staged.get("stagedUploadsCreate") or {}).get("stagedTargets") or []
    if not targets:
        return None, "Error at stage=stage_upload: no stagedTargets returned."
    return targets[0], None


def _attach_media(
    client: ShopifyClient, product_gid: str, alt: str, resource_url: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Attach a staged image as product media via productCreateMedia.

    Returns (media_node, None) on success, (None, error_str) on failure.
    error_str is already prefixed with `Error at stage=attach:`.
    """
    try:
        attach = client.execute(
            PRODUCT_CREATE_MEDIA,
            {
                "productId": product_gid,
                "media": [
                    {
                        "alt": alt or "",
                        "mediaContentType": "IMAGE",
                        "originalSource": resource_url,
                    }
                ],
            },
        )
    except Exception as e:
        return None, f"Error at stage=attach: {e}"
    attach_errors = extract_user_errors(attach, "productCreateMedia", error_key="mediaUserErrors")
    if attach_errors:
        return None, _fmt_media_user_errors(attach_errors, "attach")
    attached = (attach.get("productCreateMedia") or {}).get("media") or []
    if not attached:
        return None, "Error at stage=attach: productCreateMedia returned no media."
    return attached[0], None


def _maybe_reorder_new_media(
    client: ShopifyClient,
    product_gid: str,
    new_media_id: str,
    position: int,
    current_count: int,
) -> str:
    """Reorder a just-attached media to the caller-requested position.

    Returns a note string (possibly empty) to append to the CONFIRMED
    response. A reorder failure is reported as a `Reorder    : FAILED ...`
    line rather than an outright error — the attach already succeeded and
    we don't want to erase that.
    """
    if not position or position == current_count + 1:
        return ""
    try:
        reorder = client.execute(
            PRODUCT_REORDER_MEDIA,
            {
                "id": product_gid,
                "moves": [
                    {
                        "id": new_media_id,
                        "newPosition": str(position - 1),
                    }
                ],
            },
        )
    except Exception as e:
        return f"\n  Reorder    : FAILED at stage=reorder ({e})"
    rpayload = reorder.get("productReorderMedia", {}) or {}
    rerrs = _extract_media_user_errors(reorder, "productReorderMedia")
    if rerrs:
        return "\n  " + _fmt_media_user_errors(rerrs, "reorder").replace("Error at ", "")
    job = rpayload.get("job") or {}
    job_id = job.get("id")
    initial_done = bool(job.get("done"))
    if job_id and not initial_done:
        pr = poll_job(client, job_id, timeout_s=JOB_POLL_TIMEOUT_S)
        return (
            f"\n  Reorder    : job {from_gid(job_id)} "
            f"done={pr['done']} elapsed={pr['elapsed_s']:.1f}s"
            + (" (timed out)" if pr["timed_out"] else "")
        )
    return "\n  Reorder    : " + (f"job {from_gid(job_id)} done=True" if job_id else "done inline")


def register(server: FastMCP, client: ShopifyClient) -> None:
    @server.tool()
    def upload_product_image(
        product_id: str,
        source: str,
        alt: str = "",
        position: int = 0,
        confirm: bool = False,
    ) -> str:
        """
        Upload an image from a public https:// URL and attach it to a product.
        v1 accepts URL sources only; local file paths are rejected.

        position is 1-indexed for caller convenience (1 = featured image).
        Pass 0 or omit to append to the end.

        On failure the response is prefixed with `Error at stage={name}:` so
        the caller knows which step of the staged-upload flow broke.
        Returns a preview unless confirm=True.
        """
        if not source:
            return "Error at stage=input: provide source (a public https:// URL)."
        parsed = urlparse(source)
        if parsed.scheme != "https" or not parsed.netloc:
            return (
                "Error at stage=input: source must be a public https:// URL "
                "(v1 does not accept http:// or local file paths)."
            )

        gid = _as_product_gid(product_id)
        if not gid:
            return "Error at stage=input: provide product_id."

        # Read current media for the preview + to compute the append-default
        # position. Only a single round-trip; acceptable for preview mode.
        # Race note: `current_count` captured here is reused to compute the
        # append-position default within this call. If a concurrent tool
        # attaches media between this read and the attach below, the
        # "append to end" default may land one-before-end instead. Not
        # worth re-reading for this cost — preview accuracy isn't a
        # correctness guarantee.
        try:
            current = client.execute(GET_PRODUCT_MEDIA, {"id": gid})
        except Exception as e:
            return f"Error at stage=read: {e}"
        product = current.get("product")
        if not product:
            return f"No product found with id {product_id}."
        current_nodes = (product.get("media") or {}).get("nodes", []) or []
        current_count = len(current_nodes)

        if position and position < 1:
            return "Error at stage=input: position must be 1-indexed (>= 1) or 0 to append."

        final_position = position if position else current_count + 1
        if final_position == current_count + 1:
            pos_note = "append to end"
        else:
            pos_note = f"position {final_position}"
            if final_position == 1:
                pos_note += " (featured)"
            elif final_position > current_count + 1:
                # Shopify silently clamps out-of-range positions to the end.
                # Annotate so the operator isn't surprised when the preview
                # says "position 100" but the image lands at position 4.
                pos_note += f" (exceeds current count — Shopify will clamp to {current_count + 1})"
        preview = (
            f"PREVIEW — Upload product image\n"
            f"  Product ID : {product_id}\n"
            f"  Source     : {source}\n"
            f"  Alt        : {alt!r}\n"
            f"  Target     : {pos_note}\n"
            f"  Current    : {current_count} media attached"
        )

        if not confirm:
            return with_confirm_hint(preview)

        # Stage 1: download bytes.
        try:
            image_bytes, filename, mime_type = _download_image(source)
        except Exception as e:
            return f"Error at stage=download: {e}"

        # Stage 2: create the staged upload target.
        target, err = _stage_upload(client, filename, mime_type, len(image_bytes))
        if err:
            return err
        assert target is not None  # _stage_upload: (None, err) xor (target, None)

        # Stage 3: PUT bytes with parameters as headers.
        try:
            _upload_bytes_to_target(target, image_bytes)
        except Exception as e:
            return f"Error at stage=stage_upload: {e}"

        # Stage 4: attach via productCreateMedia.
        resource_url = target.get("resourceUrl")
        assert resource_url, "stagedUploadsCreate success implies resourceUrl is set"
        new_media, err = _attach_media(client, gid, alt, resource_url)
        if err:
            return err
        assert new_media is not None
        new_media_id = new_media.get("id")
        assert new_media_id, "productCreateMedia success implies media id is set"
        initial_preview = ((new_media.get("preview") or {}).get("image") or {}).get("url")

        # Stage 5: poll media processing (short budget; timeout is not fatal).
        poll = _poll_media_ready(client, gid, new_media_id)
        final_status = poll["status"]
        final_preview = poll["preview_url"] or initial_preview

        # Shopify returned FAILED on processing: the media record is attached
        # to the product but unusable. Skip reorder (pointless) and surface as
        # an error with the media id so the operator can delete it. Still log
        # the attach attempt so the write trail is complete.
        if final_status == "FAILED":
            log_write(
                "upload_product_image",
                f"product={product_id} media={new_media_id} "
                f"bytes={len(image_bytes)} status=FAILED "
                f"(media attached but processing failed; caller should delete)",
            )
            return (
                f"Error at stage=process: Shopify marked the media FAILED "
                f"after processing. It is attached to the product and should "
                f"be removed.\n"
                f"  Product ID : {product_id}\n"
                f"  Media ID   : {new_media_id}\n"
                f"  Source     : {source}\n"
                f"  Suggested cleanup: "
                f"delete_product_media(product_id={product_id!r}, "
                f"media_ids=[{new_media_id!r}], confirm=True)"
            )

        # Stage 6: reorder if the caller asked for a non-append position.
        reorder_note = _maybe_reorder_new_media(
            client,
            gid,
            new_media_id,
            position,
            current_count,
        )

        log_write(
            "upload_product_image",
            f"product={product_id} media={new_media_id} "
            f"pos={position or current_count + 1} bytes={len(image_bytes)} "
            f"status={final_status}",
        )

        processing_note = ""
        if poll["timed_out"] and final_status == "PROCESSING":
            processing_note = (
                f"\n  Note       : media still PROCESSING after "
                f"{_MEDIA_PROCESSING_POLL_TIMEOUT_S}s — Shopify will finish "
                f"server-side; storefront renders PROCESSING media in most cases."
            )
        return (
            f"CONFIRMED — Upload product image\n"
            f"  Product ID : {product_id}\n"
            f"  Media ID   : {new_media_id}\n"
            f"  Status     : {final_status}\n"
            f"  Position   : {pos_note}\n"
            f"  Bytes      : {_format_bytes(len(image_bytes))} ({mime_type})\n"
            f"  Preview    : {final_preview or '(not yet available)'}"
            f"{reorder_note}"
            f"{processing_note}"
        )
