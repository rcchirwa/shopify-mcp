"""Helpers shared across multiple media tools."""

from typing import Any

from shopify_mcp.tools._gid import to_gid
from shopify_mcp.tools._response import extract_user_errors, format_field_path


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


def _fmt_media_user_errors(errors: list[dict[str, Any]], stage: str) -> str:
    """Format a media mutation's userErrors for operator-facing output.

    MediaUserError.field and UserError.field are both declared `[String!]` in
    the Admin API schema — Shopify returns a path like
    `["media", "0", "originalSource"]`, not a scalar — so the list is joined
    into a dotted path via `format_field_path` rather than interpolated raw
    (which would render as the Python list repr). A scalar string still
    renders verbatim rather than being joined character-by-character; this
    guard is defensive only, since the schema guarantees a list, and mirrors
    `publications.py`'s `_map_user_error`.
    """
    parts = []
    for e in errors:
        field = e.get("field")
        path = format_field_path(e) if isinstance(field, list) else (str(field) if field else "")
        parts.append(f"{path or '(no field)'}: {e.get('message', '')}")
    msgs = "; ".join(parts)
    return f"Error at stage={stage}: {msgs}"


def _extract_media_user_errors(result: dict, mutation_key: str) -> list:
    """Extract userErrors from a media mutation, checking mediaUserErrors first
    then falling back to userErrors. productReorderMedia can surface errors
    under either slot depending on the failure mode."""
    return extract_user_errors(
        result, mutation_key, error_key="mediaUserErrors"
    ) or extract_user_errors(result, mutation_key)
