"""
Sales channel publication tools — read and manage which channels a product is
published to.

Requires OAuth scopes `read_publications` (reads) and `write_publications`
(publish/unpublish). If the app was installed before these scopes were added,
it must be reinstalled on the store.

Write operations require confirm=True and log to aon_mcp_log.txt.
"""

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient, to_gid, from_gid
from tools._log import log_write


LIST_PUBLICATIONS = """
query ListPublications($first: Int!) {
  publications(first: $first) {
    nodes {
      id
      name
      supportsFuturePublishing
    }
  }
}
"""

GET_PRODUCT_PUBLICATIONS_BY_ID = """
query GetProductPublicationsById($id: ID!) {
  product(id: $id) {
    id
    title
    handle
    resourcePublications(first: 50) {
      nodes {
        publication { id name }
        publishDate
        isPublished
      }
    }
  }
}
"""

GET_PRODUCT_PUBLICATIONS_BY_HANDLE = """
query GetProductPublicationsByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    handle
    resourcePublications(first: 50) {
      nodes {
        publication { id name }
        publishDate
        isPublished
      }
    }
  }
}
"""

PUBLISHABLE_PUBLISH = """
mutation PublishableProductPublish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    publishable { ... on Product { id title } }
    userErrors { field message }
  }
}
"""

PUBLISHABLE_UNPUBLISH = """
mutation PublishableProductUnpublish($id: ID!, $input: [PublicationInput!]!) {
  publishableUnpublish(id: $id, input: $input) {
    publishable { ... on Product { id title } }
    userErrors { field message }
  }
}
"""

SCOPE_HINT = (
    "If this is a scope error, the app likely needs reinstall on the store "
    "with read_publications / write_publications granted."
)


def _load_channels(client: ShopifyClient, cache: dict) -> list:
    """Load publications from Shopify, populate cache, return raw list."""
    data = client.execute(LIST_PUBLICATIONS, {"first": 50})
    nodes = data.get("publications", {}).get("nodes", []) or []
    cache["by_lower_name"] = {n["name"].lower(): n for n in nodes}
    cache["by_id"] = {n["id"]: n for n in nodes}
    cache["loaded"] = True
    return nodes


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
        _load_channels(client, cache)
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
    Returns (resolved: list[node], failed: list[dict])."""
    _ensure_channels(client, cache)
    resolved = []
    failed = []
    for pid in pub_ids:
        node = cache["by_id"].get(pid)
        if node:
            resolved.append(node)
        else:
            failed.append({
                "channel_name": pid,
                "error": "publication id not found on this store",
            })
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


def _resolve_product_gid_and_meta(client: ShopifyClient, product_id: str, handle: str):
    """Returns (gid, title, handle, current_published_nodes) or (None, ...)."""
    if product_id:
        data = client.execute(
            GET_PRODUCT_PUBLICATIONS_BY_ID,
            {"id": to_gid("Product", product_id)},
        )
        p = data.get("product")
    elif handle:
        data = client.execute(
            GET_PRODUCT_PUBLICATIONS_BY_HANDLE,
            {"handle": handle},
        )
        p = data.get("productByHandle")
    else:
        return None, None, None, None
    if not p:
        return None, None, None, None
    rps = (p.get("resourcePublications") or {}).get("nodes", []) or []
    return p["id"], p["title"], p["handle"], rps


def _split_current(rps: list) -> tuple:
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


def _render_channel_lines(nodes: list, extra_key: str = None) -> str:
    if not nodes:
        return "  (none)"
    lines = []
    for n in nodes:
        suffix = ""
        if extra_key and n.get(extra_key):
            suffix = f" — {extra_key}: {n[extra_key]}"
        lines.append(f"  • {n['name']} (id: {from_gid(n['id'])}){suffix}")
    return "\n".join(lines)


def register(server: FastMCP, client: ShopifyClient):
    channel_cache: dict = {}

    @server.tool()
    def list_sales_channels() -> str:
        """List every sales channel (publication) on the store."""
        try:
            nodes = _load_channels(client, channel_cache)
        except Exception as e:
            return f"Error: {e}\n{SCOPE_HINT}"
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
        """Show which sales channels a product is published to, and which it is not."""
        if not product_id and not handle:
            return "Provide either product_id or handle."
        try:
            _ensure_channels(client, channel_cache)
        except Exception as e:
            return f"Error loading sales channels: {e}\n{SCOPE_HINT}"

        try:
            gid, title, prod_handle, rps = _resolve_product_gid_and_meta(
                client, product_id, handle
            )
        except Exception as e:
            return f"Error: {e}\n{SCOPE_HINT}"

        if not gid:
            return "No product found."

        published_ids, _ = _split_current(rps)
        by_id = {(rp.get("publication") or {}).get("id"): rp for rp in rps}

        published_nodes = []
        for pid in published_ids:
            rp = by_id.get(pid, {})
            pub = rp.get("publication") or {}
            published_nodes.append({
                "id": pub.get("id"),
                "name": pub.get("name"),
                "publishDate": rp.get("publishDate"),
            })

        all_ids = set(channel_cache["by_id"].keys())
        not_published_ids = all_ids - published_ids
        not_published_nodes = [
            {"id": pid, "name": channel_cache["by_id"][pid]["name"]}
            for pid in not_published_ids
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

    def _resolve_target_nodes(channel_names: list, publication_ids: list):
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
        channel_names: list[str] = None,
        publication_ids: list[str] = None,
        confirm: bool = False,
    ) -> str:
        """
        Publish a product to one or more sales channels. Idempotent — republishing
        an already-published channel is reported as unchanged, not an error.
        Returns a preview unless confirm=True.
        """
        if not product_id and not handle:
            return "Provide either product_id or handle."
        channel_names = channel_names or []
        publication_ids = publication_ids or []

        try:
            targets, failed = _resolve_target_nodes(channel_names, publication_ids)
        except Exception as e:
            return f"Error resolving channels: {e}\n{SCOPE_HINT}"
        if targets is None:
            return "Error: " + "; ".join(f.get("error", "") for f in failed)

        try:
            gid, title, prod_handle, rps = _resolve_product_gid_and_meta(
                client, product_id, handle
            )
        except Exception as e:
            return f"Error: {e}\n{SCOPE_HINT}"
        if not gid:
            return "No product found."

        published_ids, _ = _split_current(rps)
        to_publish = [t for t in targets if t["id"] not in published_ids]
        unchanged = [t for t in targets if t["id"] in published_ids]

        preview = (
            f"PREVIEW — Publish product to channels\n"
            f"  Product: {title} (handle: {prod_handle}, id: {from_gid(gid)})\n"
            f"  Would publish to:\n{_render_channel_lines(to_publish)}\n"
            f"  Already published (unchanged):\n{_render_channel_lines(unchanged)}"
        )
        if failed:
            preview += (
                f"\n  Failed to resolve:\n" +
                "\n".join(f"  • {f.get('channel_name', '?')}: {f.get('error')}" for f in failed)
            )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        now_published = []
        apply_failed = list(failed)
        if to_publish:
            inputs = [{"publicationId": t["id"]} for t in to_publish]
            try:
                result = client.execute(PUBLISHABLE_PUBLISH, {"id": gid, "input": inputs})
            except Exception as e:
                return f"Error: {e}\n{SCOPE_HINT}"
            user_errors = result.get("publishablePublish", {}).get("userErrors", []) or []
            if user_errors:
                for ue in user_errors:
                    apply_failed.append(_map_user_error(ue, to_publish))
            else:
                now_published = to_publish

        log_write(
            "publish_product_to_channels",
            f"id={from_gid(gid)} | now_published={[n['name'] for n in now_published]} | "
            f"unchanged={[n['name'] for n in unchanged]} | failed={len(apply_failed)}",
        )

        body = (
            f"CONFIRMED — Publish product to channels\n"
            f"  Product: {title} (handle: {prod_handle}, id: {from_gid(gid)})\n"
            f"  Now published to:\n{_render_channel_lines(now_published)}\n"
            f"  Unchanged:\n{_render_channel_lines(unchanged)}"
        )
        if apply_failed:
            body += (
                f"\n  Failed:\n" +
                "\n".join(
                    f"  • {f.get('channel_name', '?')}: {f.get('error')}"
                    for f in apply_failed
                )
            )
        return body

    @server.tool()
    def unpublish_product_from_channels(
        product_id: str = "",
        handle: str = "",
        channel_names: list[str] = None,
        publication_ids: list[str] = None,
        confirm: bool = False,
    ) -> str:
        """
        Unpublish a product from one or more sales channels. Idempotent —
        unpublishing an already-unpublished channel is reported as unchanged,
        not an error. Returns a preview unless confirm=True.
        """
        if not product_id and not handle:
            return "Provide either product_id or handle."
        channel_names = channel_names or []
        publication_ids = publication_ids or []

        try:
            targets, failed = _resolve_target_nodes(channel_names, publication_ids)
        except Exception as e:
            return f"Error resolving channels: {e}\n{SCOPE_HINT}"
        if targets is None:
            return "Error: " + "; ".join(f.get("error", "") for f in failed)

        try:
            gid, title, prod_handle, rps = _resolve_product_gid_and_meta(
                client, product_id, handle
            )
        except Exception as e:
            return f"Error: {e}\n{SCOPE_HINT}"
        if not gid:
            return "No product found."

        published_ids, _ = _split_current(rps)
        to_unpublish = [t for t in targets if t["id"] in published_ids]
        unchanged = [t for t in targets if t["id"] not in published_ids]

        preview = (
            f"PREVIEW — Unpublish product from channels\n"
            f"  Product: {title} (handle: {prod_handle}, id: {from_gid(gid)})\n"
            f"  Would unpublish from:\n{_render_channel_lines(to_unpublish)}\n"
            f"  Not currently published (unchanged):\n{_render_channel_lines(unchanged)}"
        )
        if failed:
            preview += (
                f"\n  Failed to resolve:\n" +
                "\n".join(f"  • {f.get('channel_name', '?')}: {f.get('error')}" for f in failed)
            )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        now_unpublished = []
        apply_failed = list(failed)
        if to_unpublish:
            inputs = [{"publicationId": t["id"]} for t in to_unpublish]
            try:
                result = client.execute(PUBLISHABLE_UNPUBLISH, {"id": gid, "input": inputs})
            except Exception as e:
                return f"Error: {e}\n{SCOPE_HINT}"
            user_errors = result.get("publishableUnpublish", {}).get("userErrors", []) or []
            if user_errors:
                for ue in user_errors:
                    apply_failed.append(_map_user_error(ue, to_unpublish))
            else:
                now_unpublished = to_unpublish

        log_write(
            "unpublish_product_from_channels",
            f"id={from_gid(gid)} | now_unpublished={[n['name'] for n in now_unpublished]} | "
            f"unchanged={[n['name'] for n in unchanged]} | failed={len(apply_failed)}",
        )

        body = (
            f"CONFIRMED — Unpublish product from channels\n"
            f"  Product: {title} (handle: {prod_handle}, id: {from_gid(gid)})\n"
            f"  Now unpublished from:\n{_render_channel_lines(now_unpublished)}\n"
            f"  Unchanged:\n{_render_channel_lines(unchanged)}"
        )
        if apply_failed:
            body += (
                f"\n  Failed:\n" +
                "\n".join(
                    f"  • {f.get('channel_name', '?')}: {f.get('error')}"
                    for f in apply_failed
                )
            )
        return body

    @server.tool()
    def set_product_publications(
        product_id: str = "",
        handle: str = "",
        channel_names: list[str] = None,
        confirm: bool = False,
    ) -> str:
        """
        Declarative — set the exact list of sales channels the product should be
        on. Publishes to missing channels, unpublishes from extras. Returns a
        preview unless confirm=True.
        """
        if not product_id and not handle:
            return "Provide either product_id or handle."
        if channel_names is None:
            return "Provide channel_names (list of channel names for the exact desired state)."

        try:
            desired_nodes, failed = _resolve_names(client, channel_cache, channel_names)
        except Exception as e:
            return f"Error resolving channels: {e}\n{SCOPE_HINT}"

        try:
            gid, title, prod_handle, rps = _resolve_product_gid_and_meta(
                client, product_id, handle
            )
        except Exception as e:
            return f"Error: {e}\n{SCOPE_HINT}"
        if not gid:
            return "No product found."

        _ensure_channels(client, channel_cache)
        desired_ids = {n["id"] for n in desired_nodes}
        published_ids, _ = _split_current(rps)

        add_ids = desired_ids - published_ids
        remove_ids = published_ids - desired_ids
        unchanged_ids = desired_ids & published_ids

        def _nodes_for(ids):
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
            preview += (
                f"\n  Failed to resolve:\n" +
                "\n".join(f"  • {f.get('channel_name', '?')}: {f.get('error')}" for f in failed)
            )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        apply_failed = list(failed)
        added_applied = []
        removed_applied = []

        if added_nodes:
            inputs = [{"publicationId": n["id"]} for n in added_nodes]
            try:
                result = client.execute(PUBLISHABLE_PUBLISH, {"id": gid, "input": inputs})
            except Exception as e:
                return f"Error during publish: {e}\n{SCOPE_HINT}"
            user_errors = result.get("publishablePublish", {}).get("userErrors", []) or []
            if user_errors:
                for ue in user_errors:
                    apply_failed.append(_map_user_error(ue, added_nodes))
            else:
                added_applied = added_nodes

        if removed_nodes:
            inputs = [{"publicationId": n["id"]} for n in removed_nodes]
            try:
                result = client.execute(PUBLISHABLE_UNPUBLISH, {"id": gid, "input": inputs})
            except Exception as e:
                return f"Error during unpublish: {e}\n{SCOPE_HINT}"
            user_errors = result.get("publishableUnpublish", {}).get("userErrors", []) or []
            if user_errors:
                for ue in user_errors:
                    apply_failed.append(_map_user_error(ue, removed_nodes))
            else:
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
            body += (
                f"\n  Failed:\n" +
                "\n".join(
                    f"  • {f.get('channel_name', '?')}: {f.get('error')}"
                    for f in apply_failed
                )
            )
        return body
