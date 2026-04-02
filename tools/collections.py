"""
Collection tools — read and update Shopify collections.

update_collection requires confirm=True.
"""

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient
from tools._log import log_write


def _resolve_collection(client: ShopifyClient, handle: str):
    """Returns (collection_type, collection) tuple or (None, None)."""
    custom = client.get("/custom_collections.json", {"handle": handle})
    cols = custom.get("custom_collections", [])
    if cols:
        return "custom_collections", cols[0]
    smart = client.get("/smart_collections.json", {"handle": handle})
    cols = smart.get("smart_collections", [])
    if cols:
        return "smart_collections", cols[0]
    return None, None


def register(server: FastMCP, client: ShopifyClient):

    @server.tool()
    def get_collection(handle: str) -> str:
        """Get collection details by handle — title and description."""
        col_type, col = _resolve_collection(client, handle)
        if not col:
            return f"No collection found with handle '{handle}'."
        desc = col.get("body_html") or "(no description)"
        return (
            f"Collection: {col['title']}\n"
            f"Handle: {col['handle']}\n"
            f"ID: {col['id']}\n"
            f"Type: {col_type}\n"
            f"Description: {desc}"
        )

    @server.tool()
    def update_collection(
        handle: str,
        new_title: str = "",
        new_description: str = "",
        confirm: bool = False,
    ) -> str:
        """
        Update collection title or description by handle.
        Returns a preview unless confirm=True.
        At least one of new_title or new_description must be provided.
        """
        if not new_title and not new_description:
            return "Provide at least one of new_title or new_description."

        col_type, col = _resolve_collection(client, handle)
        if not col:
            return f"No collection found with handle '{handle}'."

        col_id = col["id"]
        payload: dict = {}
        if new_title:
            payload["title"] = new_title
        if new_description:
            payload["body_html"] = new_description

        preview_lines = [
            f"PREVIEW — Collection update",
            f"  Handle : {handle}",
            f"  ID     : {col_id}",
        ]
        if new_title:
            preview_lines.append(f"  Title  : '{col['title']}' → '{new_title}'")
        if new_description:
            old_desc = (col.get("body_html") or "")[:80]
            preview_lines.append(f"  Desc   : '{old_desc}...' → (new description provided)")

        preview = "\n".join(preview_lines)

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        singular = "custom_collection" if col_type == "custom_collections" else "smart_collection"
        client.put(
            f"/{col_type}/{col_id}.json",
            {singular: payload},
        )
        log_write("update_collection", f"handle={handle} | changes: {list(payload.keys())}")
        return f"Done. {preview}"
