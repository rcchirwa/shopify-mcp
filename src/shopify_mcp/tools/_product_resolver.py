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


def _lookup_by_handle(
    client: ShopifyClient,
    handle: str,
    query_by_handle: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """productByHandle lookup — always one network call, never a GID wrap."""
    data = (
        ops.read_product_snapshot_by_handle(client, query_by_handle, handle)
        if query_by_handle is not None
        else ops.read_product_by_handle_min(client, handle)
    )
    product = (data or {}).get("productByHandle") or {}
    if not product:
        return None, {}
    return product.get("id"), product


def _resolve_product(
    client: ShopifyClient,
    product_id: str = "",
    *,
    handle: str = "",
    query_by_id: str | None = None,
    query_by_handle: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Resolve a numeric / Product-GID / handle input to `(gid, snapshot)`.

    Accepts, via `product_id`:
        numeric string  → wraps to gid://shopify/Product/<id>
        Product GID     → passes through unchanged
        handle string   → productByHandle lookup (always one network call)

    ...or, via the keyword-only `handle`, a handle that is looked up with
    **no** numeric classification at all. Supplying both raises `ValueError`.

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
    a GID of the wrong resource type, or both identifiers at once) before any
    network call. Does not catch exceptions raised by the underlying GraphQL
    read — those propagate to the caller.

    **Story 10.64 (T-9.5-numeric-handle) — why `handle` exists.** Shopify
    permits a purely-numeric product handle: `Product.handle` allows letters,
    hyphens and numbers, and handleize() of a product titled "2024" yields the
    handle `2024` (https://shopify.dev/docs/api/admin-graphql/latest/objects/Product).
    Such a handle is indistinguishable from a legacy numeric product ID once
    it is inside `product_id`, so `handle=` is the unambiguous channel.

    **Accepted residual, not an oversight.** Bare digits in `product_id` still
    mean "numeric ID" — that keeps every existing caller working and preserves
    the zero-network-call path above. A caller who passes a numeric *handle*
    through `product_id` therefore still resolves to the wrong product,
    silently. Closing that would mean rejecting bare numerics outright (a
    breaking change to every tool that accepts one today); the trade-off was
    made deliberately in Story 10.64. See docs/tech-debt.md.
    """
    handle_arg = handle.strip() if isinstance(handle, str) else ""
    pid_arg = product_id.strip() if isinstance(product_id, str) else ""

    if pid_arg and handle_arg:
        raise ValueError(
            "Supply product_id or handle, not both"
            f" — got product_id={_cap(pid_arg)!r} and handle={_cap(handle_arg)!r}"
        )

    if handle_arg:
        return _lookup_by_handle(client, handle_arg, query_by_handle)

    # No handle supplied — the historical single-argument contract, including
    # its exact error text, which `catalog_hygiene._resolve_product_gid` maps.
    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("product_id must be a non-empty string")
    stripped = pid_arg

    if stripped.startswith("gid://") and not stripped.startswith(_PRODUCT_GID_PREFIX):
        raise ValueError(
            "product_id must be a numeric ID, Product GID, or handle"
            f" — got non-Product GID: {_cap(stripped)!r}"
        )

    if stripped.startswith(_PRODUCT_GID_PREFIX):
        if not stripped[len(_PRODUCT_GID_PREFIX) :]:
            raise ValueError(f"Empty product GID body: {_cap(stripped)!r}")
        gid = stripped
    elif stripped.isascii() and stripped.isdigit():
        # `.isascii()` guard: `str.isdigit()` is Unicode-aware and returns True
        # for superscript ('²'), fullwidth (U+FF10..U+FF19) and Arabic-Indic
        # ('٤٢') digits, which would be wrapped into a malformed
        # `gid://shopify/Product/²`. Shopify IDs are ASCII, so those fall
        # through to the handle lookup below and simply do not resolve.
        gid = to_gid("Product", stripped)
    else:
        return _lookup_by_handle(client, stripped, query_by_handle)

    if query_by_id is None:
        return gid, {}
    data = ops.read_product_snapshot_by_id(client, query_by_id, gid)
    product = (data or {}).get("product") or {}
    if not product:
        return None, {}
    return product.get("id") or gid, product
