"""
Collection tools — read and update Shopify collections, including
membership writes (add / remove a product from a manual collection).

All write operations require confirm=True.

Smart (rule-based) collections are rejected by the membership tools because
their contents are driven by rules; direct membership writes have no effect.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify.operations import collections as ops
from shopify.queries.collections import (
    ADD_PRODUCTS_TO_COLLECTION,
    GET_COLLECTION_BY_HANDLE,
    REMOVE_PRODUCTS_FROM_COLLECTION,
    UPDATE_COLLECTION,
)
from shopify_client import ShopifyClient, poll_job
from tools._filters import (
    format_strip_block,
    html_safety_findings,
    html_strip_report,
    sanitize_html,
)
from tools._gid import from_gid
from tools._log import log_write
from tools._response import format_user_errors, with_confirm_hint
from tools._write_tool import write_gate

# The GraphQL strings now live in shopify.queries.collections. They are
# re-exported here so existing callers/tests (`from tools.collections import
# GET_COLLECTION_BY_HANDLE`) keep resolving to the same objects the operations
# layer executes.
__all__ = [
    "ADD_PRODUCTS_TO_COLLECTION",
    "GET_COLLECTION_BY_HANDLE",
    "REMOVE_PRODUCTS_FROM_COLLECTION",
    "UPDATE_COLLECTION",
    "register",
]

# Dispatch table for the add / remove membership tools. Single source of
# truth — verbs, preposition, tool_name, the operation, and result_key are all
# keyed off the same direction so the two paths can't drift apart. Typed
# `dict[str, Any]` because the values are heterogeneous (str labels + the
# operation callable).
_MEMBERSHIP_OPS: dict[str, dict[str, Any]] = {
    "add": {
        "tool_name": "add_product_to_collection",
        "present_verb": "Add",
        "past_verb": "Added",
        "preposition": "to",
        "op": ops.add_products_to_collection,
        "result_key": "collectionAddProductsV2",
    },
    "remove": {
        "tool_name": "remove_product_from_collection",
        "present_verb": "Remove",
        "past_verb": "Removed",
        "preposition": "from",
        "op": ops.remove_products_from_collection,
        "result_key": "collectionRemoveProducts",
    },
}


def _resolve_collection(
    client: ShopifyClient, handle: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Returns (collection_type, collection) tuple or (None, None)."""
    col = ops.read_collection_by_handle(client, handle)
    if not col:
        return None, None
    col_type = "smart" if col.get("ruleSet") else "manual"
    return col_type, col


def register(server: FastMCP, client: ShopifyClient) -> None:

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

        _col_type, col = _resolve_collection(client, handle)
        if not col:
            return f"No collection found with handle '{handle}'."

        col_id = col["id"]
        # None (not "") means "not provided" — see ops.update_collection's
        # docstring on why an explicit empty sanitized description must still
        # be written rather than treated as a no-op.
        sanitized_description = sanitize_html(new_description) if new_description else None

        preview_lines = [
            "PREVIEW — Collection update",
            f"  Handle : {handle}",
            f"  ID     : {from_gid(col_id)}",
        ]
        if new_title:
            preview_lines.append(f"  Title  : '{col['title']}' → '{new_title}'")
        if new_description:
            raw_old = col.get("descriptionHtml") or ""
            old_desc_excerpt = raw_old[:80] + ("..." if len(raw_old) > 80 else "")
            danger = html_safety_findings(new_description)
            warning_suffix = (
                (
                    "\n\n⚠ DANGEROUS HTML DETECTED in new description:\n"
                    + "\n".join(f"  • {p!r}" for p in danger)
                    + "\nStorefront themes render descriptionHtml without escaping."
                )
                if danger
                else ""
            )
            stripped = html_strip_report(new_description, sanitized_description)
            strip_suffix = format_strip_block(stripped)
            preview_lines.append(
                f"  Old desc (excerpt): '{old_desc_excerpt}'\n"
                f"  New desc (full)   :\n{new_description}" + warning_suffix + strip_suffix
            )

        preview = "\n".join(preview_lines)

        # Mirror the fields the operation will put in the mutation input, only
        # to label the audit-log line — input-building itself lives in the op.
        changed_fields = [
            field
            for field, value in (("title", new_title), ("descriptionHtml", new_description))
            if value
        ]

        return write_gate(
            preview=preview,
            confirm=confirm,
            execute=lambda: ops.update_collection(
                client,
                col_id,
                new_title=new_title,
                new_description=sanitized_description,
            ),
            mutation_key="collectionUpdate",
            log_name="update_collection",
            log_description=f"handle={handle} | changes: {changed_fields}",
        )

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

        preview = (
            f"PREVIEW — {op['present_verb']} product {op['preposition']} collection\n"
            f"  Collection : {col['title']} (handle: {handle}, id: {from_gid(col_id)})\n"
            f"  Product    : {product_id}"
        )

        if not confirm:
            return with_confirm_hint(preview)

        result = op["op"](client, col_id, product_id)
        payload = result.get(op["result_key"], {}) or {}
        err = format_user_errors(result, op["result_key"])
        if err:
            return err

        job = payload.get("job") or {}
        job_id = job.get("id")
        initial_done = bool(job.get("done"))

        # Only poll when the mutation reports the job still running. When
        # `done=true` is already in the first response (typical for single-
        # product writes), the extra round-trip is pure overhead.
        poll_result = None
        if job_id and not initial_done:
            poll_result = poll_job(client, job_id)

        final_done = poll_result["done"] if poll_result else initial_done
        elapsed_s = poll_result["elapsed_s"] if poll_result else 0.0
        poll_error = poll_result["error"] if poll_result else None
        timed_out = bool(poll_result and poll_result["timed_out"])

        log_write(
            op["tool_name"],
            f"handle={handle} | product_id={product_id} | "
            f"job={job_id or '(none)'} done={final_done} "
            f"elapsed={elapsed_s:.1f}s",
        )

        body = f"Done. {op['past_verb']} product {op['preposition']} collection.\n{preview}"
        if job_id:
            numeric = from_gid(job_id)
            if poll_result is None:
                body += f"\n  Job        : {numeric} (done=True)"
            elif final_done:
                body += f"\n  Job        : {numeric} (done=True after {elapsed_s:.1f}s)"
            elif timed_out and poll_error:
                body += (
                    f"\n  Job        : {numeric} (poll failed: {poll_error} — "
                    f"underlying write succeeded, check server-side for completion)"
                )
            elif timed_out:
                body += (
                    f"\n  Job        : {numeric} (done=False, still running "
                    f"server-side after {client._settings.job_poll_timeout_s:g}s timeout — "
                    f"operation likely completed, verify via get_collection)"
                )
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
