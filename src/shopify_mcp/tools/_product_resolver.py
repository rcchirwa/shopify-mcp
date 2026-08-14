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
a different input contract, not a copy of this dispatch. Note that Story 10.64
added a `handle` parameter here, so the "separate `product_id`/`handle`
params" half of that survivor's justification no longer distinguishes it;
only its numeric-ID-only path (no GID passthrough, no handle detection) still
does. **docs/tech-debt.md is the single source of truth** — its
T-9.5-resolver-fanout entry for the behavioral differences across the six old
twins, its T-9.5-numeric-handle entry for the `handle` channel and the
residual it leaves open. Don't re-enumerate either here or in
`_resolve_product_gid`'s docstring; update those entries instead and let the
docstrings keep pointing at them.

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

    ...or, via the keyword-only `handle`, an identifier that is **never**
    numeric-classified: bare digits there always mean a handle. A Product GID
    is still accepted and passed through on that channel (a GID is never a
    valid handle, so there is no ambiguity to protect against, and rejecting
    it would only cost a wasted lookup). Supplying both raises `ValueError`.

    `query_by_id` / `query_by_handle` are the caller's own snapshot-shaped
    GraphQL query strings. When `query_by_id` is omitted, a numeric/GID input
    resolves with **zero network calls** and an empty `{}` snapshot — matching
    callers that only need the GID. When `query_by_handle` is omitted, the
    handle path still issues one call, using the minimal by-handle-only query
    (a handle can only be turned into a GID via Shopify).

    Returns `(gid, snapshot)` on success (`snapshot` is `{}` when no query was
    supplied), or `(None, {})` when the product does not exist — not-found is
    not an error at this layer, callers decide how to report it.

    Raises `ValueError` on malformed input (empty/non-string, non-string
    `handle`, empty GID body, a GID of the wrong resource type, or both
    identifiers at once) before any network call. Does not catch exceptions
    raised by the underlying GraphQL read — those propagate to the caller.

    Story 10.64 (T-9.5-numeric-handle) added the `handle` channel because
    Shopify permits a purely-numeric product handle. **docs/tech-debt.md's
    T-9.5-numeric-handle closure entry is the single source of truth** for
    that rationale and for the accepted residual it leaves open — per the
    module docstring above, don't re-enumerate either here.
    """
    if not isinstance(handle, str) and handle is not None:
        raise ValueError(f"handle must be a string — got {type(handle).__name__}")

    handle_arg = handle.strip() if isinstance(handle, str) else ""
    pid_arg = product_id.strip() if isinstance(product_id, str) else ""

    if pid_arg and handle_arg:
        raise ValueError(
            "Supply product_id or handle, not both"
            f" — got product_id={_cap(pid_arg)!r} and handle={_cap(handle_arg)!r}"
        )

    # One classifier for both channels. `numeric_is_id` is the *only*
    # difference between them: on the `handle` channel bare digits are a
    # handle, on `product_id` they are a legacy product ID.
    stripped = handle_arg or pid_arg
    if not stripped:
        # Preserves the historical single-argument error text, which
        # `catalog_hygiene._resolve_product_gid` maps verbatim.
        raise ValueError("product_id must be a non-empty string")

    if stripped.startswith("gid://") and not stripped.startswith(_PRODUCT_GID_PREFIX):
        raise ValueError(
            "product_id must be a numeric ID, Product GID, or handle"
            f" — got non-Product GID: {_cap(stripped)!r}"
        )

    if stripped.startswith(_PRODUCT_GID_PREFIX):
        if not stripped[len(_PRODUCT_GID_PREFIX) :]:
            raise ValueError(f"Empty product GID body: {_cap(stripped)!r}")
        gid = stripped
    elif not handle_arg and stripped.isascii() and stripped.isdigit():
        # Shopify permits an all-digit product handle (Product.handle allows
        # letters, hyphens and numbers — https://shopify.dev/docs/api/admin-graphql/latest/objects/Product),
        # so bare digits are only a product ID on the `product_id` channel.
        #
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
