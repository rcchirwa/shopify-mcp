"""Shared product_id resolver — numeric / GID / handle dispatch.

Story 10.62 (T-9.5-resolver-fanout): six near-twin `_resolve_product*`
helpers in `tools/catalog_hygiene.py` re-implemented the same numeric-string
/ Product-GID / handle dispatch, differing mainly in which GraphQL query they
ran and in their return shape. `_resolve_product` below is now the single
place that dispatch lives; `query_by_id` / `query_by_handle` let each caller
supply its own snapshot projection instead of copying the dispatch logic.

This function raises `ValueError` on bad input and lets transport errors
propagate — `catalog_hygiene._resolve_product_gid` survives as a thin adapter
reshaping that into its historical `(gid, error_str)` shape so none of its
four callers' observable behavior changes. `tools/publications.py`'s
`_resolve_product_gid_and_meta` is a deliberate, documented non-participant —
a different input contract (separate `product_id`/`handle` params, numeric-ID
only), not a copy of this dispatch. **docs/tech-debt.md's T-9.5-resolver-fanout
closure entry is the single source of truth** for the full list of behavioral
differences found across the six old twins and the decision made about each —
don't re-enumerate them here or in `_resolve_product_gid`'s docstring; update
that entry instead and let both docstrings keep pointing at it.

This module lives under `tools/` (not inside `catalog_hygiene.py`)
specifically so `publications.py` — or any future tool module — can import
`_resolve_product` without a layering violation, should its contract ever
narrow to match.
"""

from typing import Any

from shopify_mcp.client import ShopifyClient
from shopify_mcp.shopify.operations import catalog_hygiene as ops
from shopify_mcp.tools._gid import to_gid
from shopify_mcp.tools._scrub import cap

_PRODUCT_GID_PREFIX = "gid://shopify/Product/"
# Same 200-char bound `catalog_hygiene._cap` uses — cap user-supplied text
# reflected into error messages / logs to prevent log-flood from
# attacker-controlled input that starts with "gid://".
_GID_DISPLAY_MAX = 200


def _cap(s: str) -> str:
    return cap(s, _GID_DISPLAY_MAX)


def _resolve_product(
    client: ShopifyClient,
    product_id: str,
    *,
    query_by_id: str | None = None,
    query_by_handle: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Resolve a numeric / Product-GID / handle `product_id` to `(gid, snapshot)`.

    Accepts:
        numeric string  → wraps to gid://shopify/Product/<id>
        Product GID     → passes through unchanged
        handle string   → productByHandle lookup (always one network call)

    `query_by_id` / `query_by_handle` are the caller's own snapshot-shaped
    GraphQL query strings. When `query_by_id` is omitted, a numeric/GID input
    resolves with **zero network calls** and an empty `{}` snapshot — matching
    callers that only need the GID. When `query_by_handle` is omitted, the
    handle path still issues one call, using the minimal by-handle-only query
    (a handle can only be turned into a GID via Shopify).

    Returns `(gid, snapshot)` on success (`snapshot` is `{}` when no query was
    supplied), or `(None, {})` when the product does not exist — not-found is
    not an error at this layer, callers decide how to report it.

    Raises `ValueError` on malformed input (empty/non-string, empty GID body,
    a GID of the wrong resource type) before any network call. Does not catch
    exceptions raised by the underlying GraphQL read — those propagate to the
    caller.
    """
    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("product_id must be a non-empty string")
    stripped = product_id.strip()

    if stripped.startswith("gid://") and not stripped.startswith(_PRODUCT_GID_PREFIX):
        raise ValueError(
            "product_id must be a numeric ID, Product GID, or handle"
            f" — got non-Product GID: {_cap(stripped)!r}"
        )

    if stripped.startswith(_PRODUCT_GID_PREFIX):
        if not stripped[len(_PRODUCT_GID_PREFIX) :]:
            raise ValueError(f"Empty product GID body: {_cap(stripped)!r}")
        gid = stripped
    elif stripped.isdigit():
        gid = to_gid("Product", stripped)
    else:
        data = (
            ops.read_product_snapshot_by_handle(client, query_by_handle, stripped)
            if query_by_handle is not None
            else ops.read_product_by_handle_min(client, stripped)
        )
        product = (data or {}).get("productByHandle") or {}
        if not product:
            return None, {}
        return product.get("id"), product

    if query_by_id is None:
        return gid, {}
    data = ops.read_product_snapshot_by_id(client, query_by_id, gid)
    product = (data or {}).get("product") or {}
    if not product:
        return None, {}
    return product.get("id") or gid, product
