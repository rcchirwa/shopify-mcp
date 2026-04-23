"""
Media tools — list, upload, reorder, update, and delete product media.

All write operations require confirm=True and log to aon_mcp_log.txt.

Upload flow is staged: stagedUploadsCreate -> HTTP PUT to signed target ->
productCreateMedia attaches the resourceUrl. Per the Shopify 2026-01 spec
(see https://shopify.dev/docs/apps/build/online-store/product-media),
image uploads use HTTP PUT with the returned `parameters` applied as headers,
not multipart form fields — that shape is reserved for video / 3D model uploads,
which are out of scope for v1.
"""

import ipaddress
import mimetypes
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from mcp.server.fastmcp import FastMCP

from shopify_client import (
    ShopifyClient,
    extract_user_errors,
    from_gid,
    poll_job,
    to_gid,
    with_confirm_hint,
)
from tools._log import log_write

# Shopify caps product images at 20 MB. Reject earlier than Shopify would to
# avoid uploading bytes that can't be attached.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# Budget for the download step. Large files over slow links blow through this —
# acceptable for v1, caller can retry.
_IMAGE_DOWNLOAD_TIMEOUT_S = 30

# Budget for the reorder job poll (same as collections.py — single-item moves
# complete inline, this is only for jobs that genuinely run async).
_JOB_POLL_TIMEOUT_S = 10

# Budget for waiting on newly-attached media to leave PROCESSING. Shopify
# processing regularly exceeds any reasonable synchronous wait; we keep the
# budget short and return PROCESSING (not an error) on timeout since the
# storefront renders PROCESSING media in most cases.
_MEDIA_PROCESSING_POLL_TIMEOUT_S = 15
_MEDIA_PROCESSING_POLL_INTERVAL_S = 2.0

# Shopify's `media` connection page cap. A product with more than this in one
# request needs pagination; emit an at-cap warning so operators see the
# truncation instead of silently missing media.
_MEDIA_PAGE_CAP = 100


# --- GraphQL -----------------------------------------------------------------

GET_PRODUCT_MEDIA = """
query GetProductMedia($id: ID!) {
  product(id: $id) {
    id
    title
    media(first: 100) {
      nodes {
        id
        alt
        mediaContentType
        status
        preview { image { url } }
      }
      pageInfo { hasNextPage }
    }
  }
}
"""

# Targeted per-media read used by _poll_media_ready. Reading just the one node
# beats refetching the full media list every tick on media-heavy products.
# Every media type exposes `status` and `preview`, but only through the
# per-type inline fragments — `node` itself returns the generic `Node`
# interface, which has neither field.
GET_MEDIA_STATUS = """
query GetMediaStatus($id: ID!) {
  node(id: $id) {
    ... on MediaImage { id status preview { image { url } } }
    ... on Video { id status preview { image { url } } }
    ... on Model3d { id status preview { image { url } } }
    ... on ExternalVideo { id status preview { image { url } } }
  }
}
"""

STAGED_UPLOADS_CREATE = """
mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters { name value }
    }
    userErrors { field message }
  }
}
"""

PRODUCT_CREATE_MEDIA = """
mutation ProductCreateMedia($productId: ID!, $media: [CreateMediaInput!]!) {
  productCreateMedia(productId: $productId, media: $media) {
    media {
      id
      alt
      mediaContentType
      status
      preview { image { url } }
    }
    mediaUserErrors { field message }
  }
}
"""

PRODUCT_REORDER_MEDIA = """
mutation ProductReorderMedia($id: ID!, $moves: [MoveInput!]!) {
  productReorderMedia(id: $id, moves: $moves) {
    job { id done }
    mediaUserErrors { field message }
    userErrors { field message }
  }
}
"""

PRODUCT_UPDATE_MEDIA = """
mutation ProductUpdateMedia($productId: ID!, $media: [UpdateMediaInput!]!) {
  productUpdateMedia(productId: $productId, media: $media) {
    media { id alt }
    mediaUserErrors { field message }
  }
}
"""

PRODUCT_DELETE_MEDIA = """
mutation ProductDeleteMedia($productId: ID!, $mediaIds: [ID!]!) {
  productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
    deletedMediaIds
    product { id }
    mediaUserErrors { field message }
  }
}
"""


# --- Helpers -----------------------------------------------------------------


def _as_product_gid(pid: str) -> str:
    """Normalize a product_id arg that may arrive as numeric string or full GID.

    Returns `""` when the input is missing OR when it's a gid of the wrong
    type (e.g. `gid://shopify/Order/1`) — defense in depth against a caller
    accidentally targeting the wrong resource. Numeric ids are wrapped into
    a Product gid; Product gids pass through unchanged.
    """
    if not pid:
        return ""
    if pid.startswith("gid://shopify/Product/"):
        return pid
    if pid.startswith("gid://"):
        # Wrong gid type — refuse rather than letting it reach Shopify.
        return ""
    return to_gid("Product", pid)


def _fmt_media_user_errors(errors, stage: str) -> str:
    msgs = "; ".join(f"{e.get('field') or '(no field)'}: {e.get('message', '')}" for e in errors)
    return f"Error at stage={stage}: {msgs}"


def _extract_media_user_errors(result: dict, mutation_key: str) -> list:
    """Extract userErrors from a media mutation, checking mediaUserErrors first
    then falling back to userErrors. productReorderMedia can surface errors
    under either slot depending on the failure mode."""
    return extract_user_errors(
        result, mutation_key, error_key="mediaUserErrors"
    ) or extract_user_errors(result, mutation_key)


def _format_bytes(n) -> str:
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


def _reject_if_private_host(url: str) -> None:
    """Raise RuntimeError if the URL's hostname resolves to a non-public IP
    (RFC1918 private, loopback, link-local, multicast, reserved, unspecified).

    Bounded SSRF defense: without this, a prompt-injected caller could
    target 169.254.169.254 (cloud IMDS), 10/8 / 172.16/12 / 192.168/16
    internals, or localhost via any `https://` URL. The confirm/preview gate
    and `image/*` MIME filter narrow the exfil surface but don't close it —
    this closes it at the network boundary. TOCTOU against DNS rebinding
    is out of scope; that fix requires pinning the resolved IP through the
    request, which is not worth the complexity for this threat model.
    """
    host = urlparse(url).hostname
    if not host:
        raise RuntimeError("URL has no hostname")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise RuntimeError(f"could not resolve host {host!r}: {e}") from e
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise RuntimeError(
                f"host {host!r} resolves to non-public IP {ip_str} "
                f"— blocked to prevent SSRF to internal resources"
            )


def _download_image(url: str):
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


def _stage_upload(client: ShopifyClient, filename: str, mime_type: str, size: int):
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


def _attach_media(client: ShopifyClient, product_gid: str, alt: str, resource_url: str):
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
        pr = poll_job(client, job_id, timeout_s=_JOB_POLL_TIMEOUT_S)
        return (
            f"\n  Reorder    : job {from_gid(job_id)} "
            f"done={pr['done']} elapsed={pr['elapsed_s']:.1f}s"
            + (" (timed out)" if pr["timed_out"] else "")
        )
    return "\n  Reorder    : " + (f"job {from_gid(job_id)} done=True" if job_id else "done inline")


def _render_media_list(product: dict) -> str:
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


def register(server: FastMCP, client: ShopifyClient):

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

        # Stage 3: PUT bytes with parameters as headers.
        try:
            _upload_bytes_to_target(target, image_bytes)
        except Exception as e:
            return f"Error at stage=stage_upload: {e}"

        # Stage 4: attach via productCreateMedia.
        new_media, err = _attach_media(client, gid, alt, target.get("resourceUrl"))
        if err:
            return err
        new_media_id = new_media.get("id")
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
            poll_result = poll_job(client, job_id, timeout_s=_JOB_POLL_TIMEOUT_S)

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
                    f"{_JOB_POLL_TIMEOUT_S}s timeout — verify via "
                    f"list_product_media)"
                )
        return f"CONFIRMED — Reorder product media\n{body}{job_line}"

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

        data = client.execute(GET_PRODUCT_MEDIA, {"id": gid})
        product = data.get("product")
        if not product:
            return f"No product found with id {product_id}."
        nodes = (product.get("media") or {}).get("nodes", []) or []
        target = next((n for n in nodes if n.get("id") == media_id), None)
        if not target:
            return f"Error: media {media_id} is not attached to product {product_id}."
        old_alt = target.get("alt") or ""
        no_op_suffix = "  (no-op — alt unchanged)" if old_alt == alt else ""
        body = (
            f"  Product ID : {product_id}\n"
            f"  Media ID   : {media_id}\n"
            f"  Old alt    : {old_alt!r}\n"
            f"  New alt    : {alt!r}{no_op_suffix}"
        )

        if not confirm:
            return with_confirm_hint(f"PREVIEW — Update product media alt\n{body}")

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
        return f"CONFIRMED — Update product media alt\n{body}"

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
