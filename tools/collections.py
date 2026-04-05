"""
Collection tools — read and update Shopify collections.

update_collection requires confirm=True.
"""

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient, to_gid, from_gid
from tools._log import log_write

GET_COLLECTION_BY_HANDLE = """
query GetCollectionByHandle($handle: String!) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    descriptionHtml
    ruleSet { appliedDisjunctively }
  }
}
"""

UPDATE_COLLECTION = """
mutation UpdateCollection($input: CollectionInput!) {
  collectionUpdate(input: $input) {
    collection { id title handle }
    userErrors { field message }
  }
}
"""


def _resolve_collection(client: ShopifyClient, handle: str):
    """Returns (collection_type, collection) tuple or (None, None)."""
    data = client.execute(GET_COLLECTION_BY_HANDLE, {"handle": handle})
    col = data.get("collectionByHandle")
    if not col:
        return None, None
    col_type = "smart" if col.get("ruleSet") else "manual"
    return col_type, col


def register(server: FastMCP, client: ShopifyClient):

    @server.tool()
    def get_collection(handle: str) -> str:
        """Get collection details by handle — title and description."""
        col_type, col = _resolve_collection(client, handle)
        if not col:
            return f"No collection found with handle '{handle}'."
        desc = col.get("descriptionHtml") or "(no description)"
        return (
            f"Collection: {col['title']}\n"
            f"Handle: {col['handle']}\n"
            f"ID: {from_gid(col['id'])}\n"
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

        preview_lines = [
            f"PREVIEW — Collection update",
            f"  Handle : {handle}",
            f"  ID     : {from_gid(col_id)}",
        ]
        if new_title:
            preview_lines.append(f"  Title  : '{col['title']}' → '{new_title}'")
        if new_description:
            old_desc = (col.get("descriptionHtml") or "")[:80]
            preview_lines.append(f"  Desc   : '{old_desc}...' → (new description provided)")

        preview = "\n".join(preview_lines)

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        inp = {"id": col_id}
        if new_title:
            inp["title"] = new_title
        if new_description:
            inp["descriptionHtml"] = new_description

        result = client.execute(UPDATE_COLLECTION, {"input": inp})
        user_errors = result.get("collectionUpdate", {}).get("userErrors", [])
        if user_errors:
            msgs = "; ".join(f"{e['field']}: {e['message']}" for e in user_errors)
            return f"Error: {msgs}"

        log_write("update_collection", f"handle={handle} | changes: {[k for k in inp if k != 'id']}")
        return f"Done. {preview}"
