"""
Collection tools — read and update Shopify collections, including
membership writes (add / remove a product from a manual collection).

All write operations require confirm=True.

Smart (rule-based) collections are rejected by the membership tools because
their contents are driven by rules; direct membership writes have no effect.
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

# Both membership mutations return an async `job` in 2024-07+. For
# single-product writes the job typically completes immediately; we surface
# the job id in the response but do not block on polling.
ADD_PRODUCTS_TO_COLLECTION = """
mutation AddProductsToCollection($id: ID!, $productIds: [ID!]!) {
  collectionAddProductsV2(id: $id, productIds: $productIds) {
    job { id done }
    userErrors { field message }
  }
}
"""

REMOVE_PRODUCTS_FROM_COLLECTION = """
mutation RemoveProductsFromCollection($id: ID!, $productIds: [ID!]!) {
  collectionRemoveProducts(id: $id, productIds: $productIds) {
    job { id done }
    userErrors { field message }
  }
}
"""

# Dispatch table for the add / remove membership tools. Single source of
# truth — verbs, preposition, tool_name, mutation, and result_key are all
# keyed off the same direction so the two paths can't drift apart.
_MEMBERSHIP_OPS = {
    "add": {
        "tool_name": "add_product_to_collection",
        "present_verb": "Add",
        "past_verb": "Added",
        "preposition": "to",
        "mutation": ADD_PRODUCTS_TO_COLLECTION,
        "result_key": "collectionAddProductsV2",
    },
    "remove": {
        "tool_name": "remove_product_from_collection",
        "present_verb": "Remove",
        "past_verb": "Removed",
        "preposition": "from",
        "mutation": REMOVE_PRODUCTS_FROM_COLLECTION,
        "result_key": "collectionRemoveProducts",
    },
}


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

    def _membership_mutation(direction: str, handle: str, product_id: str, confirm: bool) -> str:
        """Shared flow for add / remove — both follow preview → confirm →
        mutation → surface-userErrors. `direction` is the only variable input;
        the mutation, response key, verbs, and preposition are all derived
        from the ops table below so the two paths can't drift.
        """
        op = _MEMBERSHIP_OPS[direction]

        if not product_id:
            return "Provide product_id."

        col_type, col = _resolve_collection(client, handle)
        if not col:
            return f"No collection found with handle '{handle}'."
        if col_type == "smart":
            return (
                f"Error: '{handle}' is a smart collection — membership is "
                f"rule-driven and cannot be changed directly."
            )

        col_id = col["id"]
        product_gid = to_gid("Product", product_id)

        preview = (
            f"PREVIEW — {op['present_verb']} product {op['preposition']} collection\n"
            f"  Collection : {col['title']} (handle: {handle}, id: {from_gid(col_id)})\n"
            f"  Product    : {product_id}"
        )

        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        result = client.execute(
            op["mutation"],
            {"id": col_id, "productIds": [product_gid]},
        )
        payload = result.get(op["result_key"], {}) or {}
        user_errors = payload.get("userErrors", []) or []
        if user_errors:
            msgs = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in user_errors)
            return f"Error: {msgs}"

        job = payload.get("job") or {}
        job_id = job.get("id")
        log_write(op["tool_name"], f"handle={handle} | product_id={product_id} | job={job_id or '(none)'}")

        body = f"Done. {op['past_verb']} product {op['preposition']} collection.\n{preview}"
        if job_id:
            body += f"\n  Job        : {from_gid(job_id)} (done={job.get('done')})"
        return body

    @server.tool()
    def add_product_to_collection(
        handle: str,
        product_id: str,
        confirm: bool = False,
    ) -> str:
        """
        Add a product to a manual collection by handle. Rejects smart
        (rule-based) collections. Returns a preview unless confirm=True.
        """
        return _membership_mutation("add", handle, product_id, confirm)

    @server.tool()
    def remove_product_from_collection(
        handle: str,
        product_id: str,
        confirm: bool = False,
    ) -> str:
        """
        Remove a product from a manual collection by handle. Rejects smart
        (rule-based) collections. Returns a preview unless confirm=True.
        """
        return _membership_mutation("remove", handle, product_id, confirm)
