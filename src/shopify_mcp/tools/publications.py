"""
Sales channel publication tools — read and manage which channels a product or a
collection is published to.

Products came first and keep the identifier pair (``product_id`` / ``handle``).
Collections were added by Story 10.83 as separate, handle-only tools rather than
by widening the product tools: ``handle`` on those means a *product* handle, and
a store can have a collection and a product sharing one handle, so overloading
the parameter would create exactly the wrong-resource hazard Story 10.68 closed.
The shared write body lives in ``_channel_write`` so publish and unpublish exist
in one copy across both resource types, not four.

Requires OAuth scopes `read_publications` (reads) and `write_publications`
(publish/unpublish). If the app was installed before these scopes were added,
it must be reinstalled on the store.

Write operations require confirm=True and log to aon_mcp_log.txt.

Thin MCP-tool surface over ``shopify.operations.publications`` (Story 10.30 / A5,
following the products pilot in Story 10.23): this module keeps the
channel-name/-id resolution cache, the publish/unpublish/declarative-set diff, the
preview/confirm flow, userError mapping, and output formatting; the GraphQL strings
live in ``shopify.queries.publications`` and the data access in
``shopify.operations.publications``.
"""

from collections.abc import Iterable
from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_mcp.client import ShopifyClient
from shopify_mcp.shopify._cache import CHANNELS
from shopify_mcp.shopify.operations import publications as ops
from shopify_mcp.shopify.queries.publications import (
    GET_COLLECTION_PUBLICATIONS_BY_HANDLE,
    GET_PRODUCT_PUBLICATIONS_BY_HANDLE,
    GET_PRODUCT_PUBLICATIONS_BY_ID,
    LIST_PUBLICATIONS,
    PUBLISHABLE_PUBLISH,
    PUBLISHABLE_UNPUBLISH,
)
from shopify_mcp.tools._gid import from_gid
from shopify_mcp.tools._log import log_write
from shopify_mcp.tools._product_resolver import identifier_error, to_gid
from shopify_mcp.tools._response import extract_user_errors, with_confirm_hint
from shopify_mcp.tools._scrub import cap

# The GraphQL strings now live in shopify.queries.publications. They are re-exported
# here so existing callers/tests (`from tools.publications import LIST_PUBLICATIONS`)
# keep resolving to the same objects the operations layer executes.
__all__ = [
    "GET_COLLECTION_PUBLICATIONS_BY_HANDLE",
    "GET_PRODUCT_PUBLICATIONS_BY_HANDLE",
    "GET_PRODUCT_PUBLICATIONS_BY_ID",
    "LIST_PUBLICATIONS",
    "PUBLISHABLE_PUBLISH",
    "PUBLISHABLE_UNPUBLISH",
    "register",
]

SCOPE_HINT = (
    "If this is a scope error, the app likely needs reinstall on the store "
    "with read_publications / write_publications granted."
)


def _read_channels(client: ShopifyClient, *, force: bool) -> list:
    """Return the raw publications node list via the client's cross-call TTL cache
    (A8 / Story 10.32). A warm, unexpired cache is served without an API call; a
    cold/expired cache — or ``force=True``, used when a requested channel name
    missed and the roster may have changed — re-reads ``LIST_PUBLICATIONS`` and
    refreshes the cache entry."""
    metadata_cache = client._metadata_cache
    if not force:
        cached = metadata_cache.get(CHANNELS)
        if cached is not None:
            return cached
    nodes = ops.read_publications(client)
    metadata_cache.set(CHANNELS, nodes)
    return nodes


def _load_channels(client: ShopifyClient, cache: dict, *, force: bool = False) -> list:
    """Load publications (cross-call cached), populate the per-call ``cache`` index,
    return the raw list. The per-call dict is still rebuilt every call so name/id
    lookups stay request-local; only the underlying API read is cached across calls."""
    nodes = _read_channels(client, force=force)
    cache["by_lower_name"] = {n["name"].lower(): n for n in nodes}
    cache["by_id"] = {n["id"]: n for n in nodes}
    cache["loaded"] = True
    return nodes


def _invalidate_channels(client: ShopifyClient) -> None:
    """Drop the cross-call channels cache after a *successful* channel-affecting
    mutation (publish/unpublish — called only when the mutation returned no
    userErrors), so the next channels read reflects any roster change
    (A8 / Story 10.32). Conservative by design: a product publish does not itself
    alter the channel roster, but invalidating here guarantees no stale-after-write
    roster is served if the roster changed around the same write."""
    client._metadata_cache.invalidate(CHANNELS)


def _ensure_channels(client: ShopifyClient, cache: dict) -> None:
    if not cache.get("loaded"):
        _load_channels(client, cache)


def _resolve_names(client: ShopifyClient, cache: dict, names: list) -> tuple:
    """Map channel names → publication nodes. Refresh cache on miss once.
    Returns (resolved: list[node], failed: list[dict])."""
    _ensure_channels(client, cache)
    resolved = []
    failed = []
    needs_refresh = any(n.lower() not in cache["by_lower_name"] for n in names)
    if needs_refresh:
        # A name missed the cache; the roster may have changed since the cross-call
        # entry was cached, so force a fresh API read (bypassing the cache) before
        # reporting the name unresolved. force=True also refreshes the cached entry.
        _load_channels(client, cache, force=True)
    for n in names:
        node = cache["by_lower_name"].get(n.lower())
        if node:
            resolved.append(node)
        else:
            failed.append({"channel_name": n, "error": "channel not found on this store"})
    return resolved, failed


def _resolve_ids(client: ShopifyClient, cache: dict, pub_ids: list) -> tuple:
    """Map publication IDs → publication nodes. Unknown IDs go to `failed`,
    mirroring `_resolve_names` so both paths short-circuit before mutating.
    Accepts both full GID form (`gid://shopify/Publication/123`) and raw
    numeric form (`"123"` or `123`) — the latter is what `get_product_publications`
    prints to the user, so copy-paste between tools works.
    Returns (resolved: list[node], failed: list[dict])."""
    _ensure_channels(client, cache)
    resolved = []
    failed = []
    for pid in pub_ids:
        pid_str = str(pid)
        pid_gid = pid_str if pid_str.startswith("gid://") else to_gid("Publication", pid_str)
        node = cache["by_id"].get(pid_gid)
        if node:
            resolved.append(node)
        else:
            failed.append(
                {
                    "channel_name": pid_str,
                    "error": "publication id not found on this store",
                }
            )
    return resolved, failed


def _map_user_error(user_error: dict, targets: list) -> dict:
    """Shopify returns userError.field like ["input", "0", "publicationId"] for
    list-shaped mutation inputs. Recover the channel name by matching the index
    back to our target list. Falls back to the raw field path on parse failure."""
    field = user_error.get("field") or []
    message = user_error.get("message")
    idx = None
    if isinstance(field, list) and len(field) >= 2:
        try:
            idx = int(field[1])
        except (ValueError, TypeError):
            idx = None
    if idx is not None and 0 <= idx < len(targets):
        return {"channel_name": targets[idx].get("name"), "error": message}
    raw = ".".join(str(f) for f in field) if isinstance(field, list) else str(field)
    return {"channel_name": raw or "(unknown)", "error": message}


def _resolve_product_gid_and_meta(
    client: ShopifyClient, product_id: str, handle: str
) -> tuple[str | None, str | None, str | None, list[dict[str, Any]]]:
    """Returns (gid, title, handle, current_published_nodes); first three
    are None when no product was resolved, rps is always a list.

    Delegates the by-id/by-handle paginated read to
    ``shopify.operations.publications.read_product_publications`` and reshapes its
    ``(product_or_None, rps, capped)`` result into the 4-tuple the tool formatting
    uses; the pagination cap is not surfaced for publications, so ``capped`` is
    dropped here exactly as before the migration.

    Raises ``ValueError`` when both identifiers are supplied — the refusal is
    enforced in the operations layer (Story 10.68), so all four tools inherit it
    through this one funnel without a per-tool check. Each already wraps this
    call in ``try/except`` and renders the message through its structured error
    path, which is also where the reflection bound (``cap``) is applied."""
    p, rps, _capped = ops.read_product_publications(client, product_id, handle)
    if not p:
        return None, None, None, []
    return p["id"], p["title"], p["handle"], rps


def _split_current(rps: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Split resourcePublications into (published_set, unpublished_set) of publication_ids."""
    published = set()
    not_published = set()
    for rp in rps:
        pid = (rp.get("publication") or {}).get("id")
        if not pid:
            continue
        if rp.get("isPublished"):
            published.add(pid)
        else:
            not_published.add(pid)
    return published, not_published


def _resolve_collection_gid_and_meta(
    client: ShopifyClient, handle: str
) -> tuple[str | None, str | None, str | None, list[dict[str, Any]]]:
    """Collection twin of :func:`_resolve_product_gid_and_meta`.

    Returns (gid, title, handle, current_published_nodes); the first three are
    None when no collection resolved, rps is always a list. Handle-only — there
    is no by-id collection query and Story 10.83 did not add one, so unlike the
    product funnel there is no identifier pair and nothing to refuse."""
    col, rps, _capped = ops.read_collection_publications(client, handle)
    if not col:
        return None, None, None, []
    return col["id"], col["title"], col["handle"], rps


# Direction table for the shared publish/unpublish body below. Single source of
# truth — the verb, the preposition, every section label, the operation, the
# response key and the log key are all derived from the direction so the two
# paths cannot drift. Same shape as `tools/collections.py::_MEMBERSHIP_OPS`.
#
# `acts_on_published` is the membership predicate: publish acts on targets the
# resource is NOT yet on, unpublish acts on the ones it IS on.
_CHANNEL_WRITE_OPS: dict[str, dict[str, Any]] = {
    "publish": {
        "verb": "Publish",
        "preposition": "to",
        "would_label": "Would publish to",
        "unchanged_label": "Already published (unchanged)",
        "done_label": "Now published to",
        "log_key": "now_published",
        "op": ops.publish,
        "result_key": "publishablePublish",
        "acts_on_published": False,
    },
    "unpublish": {
        "verb": "Unpublish",
        "preposition": "from",
        "would_label": "Would unpublish from",
        "unchanged_label": "Not currently published (unchanged)",
        "done_label": "Now unpublished from",
        "log_key": "now_unpublished",
        "op": ops.unpublish,
        "result_key": "publishableUnpublish",
        "acts_on_published": True,
    },
}


def _render_failed(failed: list[dict[str, Any]]) -> str:
    return "\n".join(f"  • {f.get('channel_name', '?')}: {f.get('error')}" for f in failed)


def _channel_write(
    client: ShopifyClient,
    *,
    direction: str,
    resource_label: str,
    meta_line: str,
    gid: str,
    rps: list[dict[str, Any]],
    targets: list,
    failed: list,
    confirm: bool,
    log_name: str,
) -> str:
    """Shared preview → confirm → mutate → map-userErrors → log → render flow for
    every channel write. `direction` and `resource_label` are the only variable
    inputs; everything else is derived from ``_CHANNEL_WRITE_OPS``.

    Story 10.83 factored this out of ``publish_product_to_channels`` and
    ``unpublish_product_from_channels``, which were already two near-identical
    copies — adding the two collection tools would have made four. The product
    tools' output is byte-identical to what they emitted before, which their
    unmodified tests pin.

    Only the published set is consulted. Shopify's ``resourcePublications``
    omits channels the resource is not on rather than listing them with
    ``isPublished: false``, so the not-published side is always derived as the
    complement over the resolved targets — never read off the response."""
    spec = _CHANNEL_WRITE_OPS[direction]
    published_ids, _ = _split_current(rps)
    acting = [t for t in targets if (t["id"] in published_ids) is spec["acts_on_published"]]
    unchanged = [t for t in targets if (t["id"] in published_ids) is not spec["acts_on_published"]]

    heading = f"{spec['verb']} {resource_label} {spec['preposition']} channels"
    preview = (
        f"PREVIEW — {heading}\n"
        f"  {meta_line}\n"
        f"  {spec['would_label']}:\n{_render_channel_lines(acting)}\n"
        f"  {spec['unchanged_label']}:\n{_render_channel_lines(unchanged)}"
    )
    if failed:
        preview += "\n  Failed to resolve:\n" + _render_failed(failed)

    if not confirm:
        return with_confirm_hint(preview)

    done: list = []
    apply_failed = list(failed)
    if acting:
        try:
            result = spec["op"](client, gid, [t["id"] for t in acting])
        except Exception as e:
            return f"Error: {cap(str(e))}\n{SCOPE_HINT}"
        user_errors = extract_user_errors(result, spec["result_key"])
        if user_errors:
            for ue in user_errors:
                apply_failed.append(_map_user_error(ue, acting))
        else:
            _invalidate_channels(client)
            done = acting

    log_write(
        log_name,
        f"id={from_gid(gid)} | {spec['log_key']}={[n['name'] for n in done]} | "
        f"unchanged={[n['name'] for n in unchanged]} | failed={len(apply_failed)}",
    )

    body = (
        f"CONFIRMED — {heading}\n"
        f"  {meta_line}\n"
        f"  {spec['done_label']}:\n{_render_channel_lines(done)}\n"
        f"  Unchanged:\n{_render_channel_lines(unchanged)}"
    )
    if apply_failed:
        body += "\n  Failed:\n" + _render_failed(apply_failed)
    return body


def _render_channel_lines(nodes: list, extra_key: str | None = None) -> str:
    if not nodes:
        return "  (none)"
    lines = []
    for n in nodes:
        suffix = ""
        if extra_key and n.get(extra_key):
            suffix = f" — {extra_key}: {n[extra_key]}"
        lines.append(f"  • {n['name']} (id: {from_gid(n['id'])}){suffix}")
    return "\n".join(lines)


def register(server: FastMCP, client: ShopifyClient) -> None:
    channel_cache: dict[str, Any] = {}

    @server.tool()
    def list_sales_channels() -> str:
        """List every sales channel (publication) on the store."""
        try:
            nodes = _load_channels(client, channel_cache)
        except Exception as e:
            return f"Error: {cap(str(e))}\n{SCOPE_HINT}"
        if not nodes:
            return "No sales channels found on this store."
        lines = [f"Sales channels ({len(nodes)} total):"]
        for n in nodes:
            supports = "yes" if n.get("supportsFuturePublishing") else "no"
            lines.append(
                f"  • {n['name']} | id: {from_gid(n['id'])} | "
                f"supports_future_publishing: {supports}"
            )
        return "\n".join(lines)

    @server.tool()
    def get_product_publications(product_id: str = "", handle: str = "") -> str:
        """Show which sales channels a product is published to, and which it is not.

        Supply exactly one of product_id / handle. Supplying both is rejected
        before the product is read rather than resolved by `product_id` with the
        `handle` silently discarded (Story 10.68 — a contract change; see the
        module docstring of `shopify._identifiers`).
        """
        # Story 10.68: vet the identifier pair FIRST — ahead of the sales-channel
        # reads below. Inheriting the refusal from the operations layer would let
        # an ambiguous call cost a round-trip, let a channel-resolution failure
        # mask the argument error, and route the message through the generic
        # handler that appends a misleading reinstall-the-app scope hint.
        err = identifier_error(product_id, handle)
        if err:
            return err
        try:
            _ensure_channels(client, channel_cache)
        except Exception as e:
            return f"Error loading sales channels: {cap(str(e))}\n{SCOPE_HINT}"

        try:
            gid, title, prod_handle, rps = _resolve_product_gid_and_meta(client, product_id, handle)
        except Exception as e:
            return f"Error: {cap(str(e))}\n{SCOPE_HINT}"

        if not gid:
            return "No product found."

        published_ids, _ = _split_current(rps)
        by_id = {(rp.get("publication") or {}).get("id"): rp for rp in rps}

        published_nodes = []
        for pid in published_ids:
            rp = by_id.get(pid, {})
            pub = rp.get("publication") or {}
            published_nodes.append(
                {
                    "id": pub.get("id"),
                    "name": pub.get("name"),
                    "publishDate": rp.get("publishDate"),
                }
            )

        all_ids = set(channel_cache["by_id"].keys())
        not_published_ids = all_ids - published_ids
        not_published_nodes = [
            {"id": pid, "name": channel_cache["by_id"][pid]["name"]} for pid in not_published_ids
        ]

        return (
            f"Product: {title}\n"
            f"Handle: {prod_handle}\n"
            f"ID: {from_gid(gid)}\n\n"
            f"Published to ({len(published_nodes)}):\n"
            f"{_render_channel_lines(published_nodes, 'publishDate')}\n\n"
            f"Not published to ({len(not_published_nodes)}):\n"
            f"{_render_channel_lines(not_published_nodes)}"
        )

    def _resolve_target_nodes(
        channel_names: list[str], publication_ids: list[str]
    ) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
        """Returns (targets: list[node], failed: list[dict])."""
        if channel_names and publication_ids:
            return None, [{"error": "provide channel_names OR publication_ids, not both"}]
        if channel_names:
            return _resolve_names(client, channel_cache, channel_names)
        if publication_ids:
            return _resolve_ids(client, channel_cache, publication_ids)
        return None, [{"error": "provide channel_names or publication_ids"}]

    @server.tool()
    def publish_product_to_channels(
        product_id: str = "",
        handle: str = "",
        channel_names: list[str] | None = None,
        publication_ids: list[str] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Publish a product to one or more sales channels. Idempotent — republishing
        an already-published channel is reported as unchanged, not an error.
        Returns a preview unless confirm=True.

        Supply exactly one of product_id / handle. **Supplying both is refused
        before the mutation** rather than resolved by `product_id` with the
        `handle` silently discarded — on a write tool that precedence could
        publish the WRONG product (Story 10.68 — a contract change; see the
        module docstring of `shopify._identifiers`).
        """
        # Story 10.68: vet the identifier pair FIRST — ahead of the sales-channel
        # reads below. Inheriting the refusal from the operations layer would let
        # an ambiguous call cost a round-trip, let a channel-resolution failure
        # mask the argument error, and route the message through the generic
        # handler that appends a misleading reinstall-the-app scope hint.
        err = identifier_error(product_id, handle)
        if err:
            return err
        channel_names = channel_names or []
        publication_ids = publication_ids or []

        try:
            targets, failed = _resolve_target_nodes(channel_names, publication_ids)
        except Exception as e:
            return f"Error resolving channels: {cap(str(e))}\n{SCOPE_HINT}"
        if targets is None:
            return "Error: " + "; ".join(f.get("error", "") for f in failed)

        try:
            gid, title, prod_handle, rps = _resolve_product_gid_and_meta(client, product_id, handle)
        except Exception as e:
            return f"Error: {cap(str(e))}\n{SCOPE_HINT}"
        if not gid:
            return "No product found."

        return _channel_write(
            client,
            direction="publish",
            resource_label="product",
            meta_line=f"Product: {title} (handle: {prod_handle}, id: {from_gid(gid)})",
            gid=gid,
            rps=rps,
            targets=targets,
            failed=failed,
            confirm=confirm,
            log_name="publish_product_to_channels",
        )

    @server.tool()
    def unpublish_product_from_channels(
        product_id: str = "",
        handle: str = "",
        channel_names: list[str] | None = None,
        publication_ids: list[str] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Unpublish a product from one or more sales channels. Idempotent —
        unpublishing an already-unpublished channel is reported as unchanged,
        not an error. Returns a preview unless confirm=True.

        Supply exactly one of product_id / handle. **Supplying both is refused
        before the mutation** rather than resolved by `product_id` with the
        `handle` silently discarded — on a write tool that precedence could
        unpublish the WRONG product (Story 10.68 — a contract change; see the
        module docstring of `shopify._identifiers`).
        """
        # Story 10.68: vet the identifier pair FIRST — ahead of the sales-channel
        # reads below. Inheriting the refusal from the operations layer would let
        # an ambiguous call cost a round-trip, let a channel-resolution failure
        # mask the argument error, and route the message through the generic
        # handler that appends a misleading reinstall-the-app scope hint.
        err = identifier_error(product_id, handle)
        if err:
            return err
        channel_names = channel_names or []
        publication_ids = publication_ids or []

        try:
            targets, failed = _resolve_target_nodes(channel_names, publication_ids)
        except Exception as e:
            return f"Error resolving channels: {cap(str(e))}\n{SCOPE_HINT}"
        if targets is None:
            return "Error: " + "; ".join(f.get("error", "") for f in failed)

        try:
            gid, title, prod_handle, rps = _resolve_product_gid_and_meta(client, product_id, handle)
        except Exception as e:
            return f"Error: {cap(str(e))}\n{SCOPE_HINT}"
        if not gid:
            return "No product found."

        return _channel_write(
            client,
            direction="unpublish",
            resource_label="product",
            meta_line=f"Product: {title} (handle: {prod_handle}, id: {from_gid(gid)})",
            gid=gid,
            rps=rps,
            targets=targets,
            failed=failed,
            confirm=confirm,
            log_name="unpublish_product_from_channels",
        )

    # ---- collection publications (Story 10.83 / T-collection-publish) ----
    #
    # Separate tools rather than widening the three shipped product tools. The
    # decisive argument is not duplication but ambiguity: `handle` on those
    # tools means a PRODUCT handle, and this store has a collection and a
    # product that can share a handle. Overloading it would create exactly the
    # wrong-resource hazard Story 10.68 was written to close. Nearly every
    # helper above is already resource-agnostic and is reused unchanged; the
    # write body is shared through `_channel_write` so there are two copies of
    # that flow, not four.
    #
    # Handle-only, matching every other collection tool. With one identifier
    # there is no pair to refuse, so these tools have no `identifier_error`
    # twin — the first statement is the empty-handle guard instead, which still
    # lands ahead of the sales-channel read as the placement rule requires.

    def _collection_target(handle: str) -> tuple[str, str | None, str | None, list]:
        """Resolve a collection for the three tools below.

        Returns (error_or_empty, gid, meta_line, rps). A non-empty first element
        is the message to return; the rest are then meaningless."""
        try:
            gid, title, col_handle, rps = _resolve_collection_gid_and_meta(client, handle)
        except Exception as e:
            return f"Error: {cap(str(e))}\n{SCOPE_HINT}", None, None, []
        if not gid:
            return "No collection found.", None, None, []
        return "", gid, f"Collection: {title} (handle: {col_handle}, id: {from_gid(gid)})", rps

    @server.tool()
    def get_collection_publications(handle: str = "") -> str:
        """
        Show which sales channels a collection is published to, and which it
        is not. Collections are named by handle only.
        """
        if not handle.strip():
            return "Provide handle."
        try:
            _ensure_channels(client, channel_cache)
        except Exception as e:
            return f"Error loading sales channels: {cap(str(e))}\n{SCOPE_HINT}"

        try:
            gid, title, col_handle, rps = _resolve_collection_gid_and_meta(client, handle.strip())
        except Exception as e:
            return f"Error: {cap(str(e))}\n{SCOPE_HINT}"
        if not gid:
            return "No collection found."

        published_ids, _ = _split_current(rps)
        by_id = {(rp.get("publication") or {}).get("id"): rp for rp in rps}

        published_nodes = []
        for pid in published_ids:
            rp = by_id.get(pid, {})
            pub = rp.get("publication") or {}
            published_nodes.append(
                {
                    "id": pub.get("id"),
                    "name": pub.get("name"),
                    "publishDate": rp.get("publishDate"),
                }
            )

        # Derived, not read off the response: resourcePublications omits the
        # channels the collection is not on (live-confirmed 2026-09-05), so the
        # complement has to come from the channel roster.
        not_published_ids = set(channel_cache["by_id"].keys()) - published_ids
        not_published_nodes = [
            {"id": pid, "name": channel_cache["by_id"][pid]["name"]} for pid in not_published_ids
        ]

        return (
            f"Collection: {title}\n"
            f"Handle: {col_handle}\n"
            f"ID: {from_gid(gid)}\n\n"
            f"Published to ({len(published_nodes)}):\n"
            f"{_render_channel_lines(published_nodes, 'publishDate')}\n\n"
            f"Not published to ({len(not_published_nodes)}):\n"
            f"{_render_channel_lines(not_published_nodes)}"
        )

    def _collection_channel_write(
        direction: str,
        handle: str,
        channel_names: list[str] | None,
        publication_ids: list[str] | None,
        confirm: bool,
        log_name: str,
    ) -> str:
        """Shared front half for the two collection write tools: vet the handle,
        resolve channels, resolve the collection, then hand off to the
        resource-agnostic `_channel_write`."""
        if not handle.strip():
            return "Provide handle."

        try:
            targets, failed = _resolve_target_nodes(channel_names or [], publication_ids or [])
        except Exception as e:
            return f"Error resolving channels: {cap(str(e))}\n{SCOPE_HINT}"
        if targets is None:
            return "Error: " + "; ".join(f.get("error", "") for f in failed)

        err, gid, meta_line, rps = _collection_target(handle.strip())
        if err:
            return err
        assert gid is not None and meta_line is not None  # narrowed by `err`

        return _channel_write(
            client,
            direction=direction,
            resource_label="collection",
            meta_line=meta_line,
            gid=gid,
            rps=rps,
            targets=targets,
            failed=failed,
            confirm=confirm,
            log_name=log_name,
        )

    @server.tool()
    def publish_collection_to_channels(
        handle: str = "",
        channel_names: list[str] | None = None,
        publication_ids: list[str] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Publish a collection to one or more sales channels. Idempotent —
        republishing an already-published channel is reported as unchanged, not
        an error. Returns a preview unless confirm=True.

        Collections are named by handle only. Works for both manual and smart
        collections; the two behave identically here.

        Publishing to "Online Store" is what makes a collection's storefront
        page reachable — a newly created collection is on no channel at all.
        """
        return _collection_channel_write(
            "publish",
            handle,
            channel_names,
            publication_ids,
            confirm,
            "publish_collection_to_channels",
        )

    @server.tool()
    def unpublish_collection_from_channels(
        handle: str = "",
        channel_names: list[str] | None = None,
        publication_ids: list[str] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Unpublish a collection from one or more sales channels. Idempotent —
        unpublishing a channel it is not on is reported as unchanged, not an
        error. Returns a preview unless confirm=True.

        Collections are named by handle only. Removing "Online Store" makes the
        collection's storefront page 404.
        """
        return _collection_channel_write(
            "unpublish",
            handle,
            channel_names,
            publication_ids,
            confirm,
            "unpublish_collection_from_channels",
        )

    @server.tool()
    def set_product_publications(
        product_id: str = "",
        handle: str = "",
        channel_names: list[str] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Declarative — set the exact list of sales channels the product should be
        on. Publishes to missing channels, unpublishes from extras. Returns a
        preview unless confirm=True.

        Supply exactly one of product_id / handle. **Supplying both is refused
        before either mutation** rather than resolved by `product_id` with the
        `handle` silently discarded — on a write tool that precedence could
        rewrite the WRONG product's channel set (Story 10.68 — a contract
        change; see the module docstring of `shopify._identifiers`).
        """
        # Story 10.68: vet the identifier pair FIRST — ahead of the sales-channel
        # reads below. Inheriting the refusal from the operations layer would let
        # an ambiguous call cost a round-trip, let a channel-resolution failure
        # mask the argument error, and route the message through the generic
        # handler that appends a misleading reinstall-the-app scope hint.
        err = identifier_error(product_id, handle)
        if err:
            return err
        if channel_names is None:
            return "Provide channel_names (list of channel names for the exact desired state)."

        try:
            desired_nodes, failed = _resolve_names(client, channel_cache, channel_names)
        except Exception as e:
            return f"Error resolving channels: {cap(str(e))}\n{SCOPE_HINT}"

        try:
            gid, title, prod_handle, rps = _resolve_product_gid_and_meta(client, product_id, handle)
        except Exception as e:
            return f"Error: {cap(str(e))}\n{SCOPE_HINT}"
        if not gid:
            return "No product found."

        _ensure_channels(client, channel_cache)
        desired_ids = {n["id"] for n in desired_nodes}
        published_ids, _ = _split_current(rps)

        add_ids = desired_ids - published_ids
        remove_ids = published_ids - desired_ids
        unchanged_ids = desired_ids & published_ids

        def _nodes_for(ids: Iterable[str]) -> list[dict[str, Any]]:
            return [channel_cache["by_id"][i] for i in ids if i in channel_cache["by_id"]]

        added_nodes = _nodes_for(add_ids)
        removed_nodes = _nodes_for(remove_ids)
        unchanged_nodes = _nodes_for(unchanged_ids)

        preview = (
            f"PREVIEW — Set product publications (declarative)\n"
            f"  Product: {title} (handle: {prod_handle}, id: {from_gid(gid)})\n"
            f"  Would add (publish):\n{_render_channel_lines(added_nodes)}\n"
            f"  Would remove (unpublish):\n{_render_channel_lines(removed_nodes)}\n"
            f"  Unchanged:\n{_render_channel_lines(unchanged_nodes)}"
        )
        if failed:
            preview += "\n  Failed to resolve:\n" + "\n".join(
                f"  • {f.get('channel_name', '?')}: {f.get('error')}" for f in failed
            )

        if not confirm:
            return with_confirm_hint(preview)

        apply_failed = list(failed)
        added_applied = []
        removed_applied = []

        if added_nodes:
            try:
                result = ops.publish(client, gid, [n["id"] for n in added_nodes])
            except Exception as e:
                return f"Error during publish: {cap(str(e))}\n{SCOPE_HINT}"
            user_errors = extract_user_errors(result, "publishablePublish")
            if user_errors:
                for ue in user_errors:
                    apply_failed.append(_map_user_error(ue, added_nodes))
            else:
                _invalidate_channels(client)
                added_applied = added_nodes

        if removed_nodes:
            try:
                result = ops.unpublish(client, gid, [n["id"] for n in removed_nodes])
            except Exception as e:
                return f"Error during unpublish: {cap(str(e))}\n{SCOPE_HINT}"
            user_errors = extract_user_errors(result, "publishableUnpublish")
            if user_errors:
                for ue in user_errors:
                    apply_failed.append(_map_user_error(ue, removed_nodes))
            else:
                _invalidate_channels(client)
                removed_applied = removed_nodes

        log_write(
            "set_product_publications",
            f"id={from_gid(gid)} | added={[n['name'] for n in added_applied]} | "
            f"removed={[n['name'] for n in removed_applied]} | "
            f"unchanged={[n['name'] for n in unchanged_nodes]} | failed={len(apply_failed)}",
        )

        body = (
            f"CONFIRMED — Set product publications (declarative)\n"
            f"  Product: {title} (handle: {prod_handle}, id: {from_gid(gid)})\n"
            f"  Added (published):\n{_render_channel_lines(added_applied)}\n"
            f"  Removed (unpublished):\n{_render_channel_lines(removed_applied)}\n"
            f"  Unchanged:\n{_render_channel_lines(unchanged_nodes)}"
        )
        if apply_failed:
            body += "\n  Failed:\n" + "\n".join(
                f"  • {f.get('channel_name', '?')}: {f.get('error')}" for f in apply_failed
            )
        return body
