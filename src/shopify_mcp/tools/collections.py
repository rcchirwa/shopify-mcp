"""
Collection tools — create, read and update Shopify collections, including
membership writes (add / remove a product from a manual collection).

All write operations require confirm=True.

Smart (rule-based) collections are rejected by the membership tools because
their contents are driven by rules; direct membership writes have no effect.
``create_collection`` therefore only makes manual collections (Story 10.82).
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_mcp.client import ShopifyClient, poll_job
from shopify_mcp.shopify.operations import collections as ops
from shopify_mcp.shopify.queries.collections import (
    ADD_PRODUCTS_TO_COLLECTION,
    CREATE_COLLECTION,
    GET_COLLECTION_BY_HANDLE,
    REMOVE_PRODUCTS_FROM_COLLECTION,
    UPDATE_COLLECTION,
)
from shopify_mcp.tools._filters import (
    format_description_warning_block,
    format_strip_block,
    html_safety_findings,
    html_strip_report,
    sanitize_html,
)
from shopify_mcp.tools._gid import from_gid
from shopify_mcp.tools._log import log_write
from shopify_mcp.tools._response import format_user_errors, with_confirm_hint
from shopify_mcp.tools._scrub import cap
from shopify_mcp.tools._untrusted import with_reminder, wrap
from shopify_mcp.tools._write_tool import write_gate
from shopify_mcp.tools.products import slugify_shopify_handle

# The GraphQL strings now live in shopify.queries.collections. They are
# re-exported here so existing callers/tests (`from tools.collections import
# GET_COLLECTION_BY_HANDLE`) keep resolving to the same objects the operations
# layer executes.
__all__ = [
    "ADD_PRODUCTS_TO_COLLECTION",
    "CREATE_COLLECTION",
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
        # Stored descriptionHtml is merchant/app/import-authored free text —
        # fence it as untrusted (Story 10.63 / SEC-04-descriptions). The
        # '(no description)' placeholder is our own text, so it stays bare.
        raw_desc = col.get("descriptionHtml") or ""
        desc = wrap(raw_desc) if raw_desc else "(no description)"
        head = (
            f"Collection: {col['title']}\n"
            f"Handle: {col['handle']}\n"
            f"ID: {from_gid(col['id'])}\n"
            f"Type: {col_type}\n"
            f"Description: {desc}"
        )
        return with_reminder(head)

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
            # Old half is stored store content (fenced); the new half is the
            # caller's own input (left raw). The truncation marker stays outside
            # the fence — it is ours, not the store's. Story 10.63.
            raw_old = col.get("descriptionHtml") or ""
            old_desc_excerpt = (
                wrap(raw_old[:80]) + ("..." if len(raw_old) > 80 else "") if raw_old else ""
            )
            danger = html_safety_findings(new_description)
            warning_suffix = format_description_warning_block(danger)
            stripped = html_strip_report(new_description, sanitized_description)
            strip_suffix = format_strip_block(stripped)
            preview_lines.append(
                f"  Old desc (excerpt): '{old_desc_excerpt}'\n"
                f"  New desc (full)   :\n{new_description}" + warning_suffix + strip_suffix
            )

        preview = with_reminder("\n".join(preview_lines))

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

    @server.tool()
    def create_collection(
        title: str,
        handle: str = "",
        description: str = "",
        confirm: bool = False,
    ) -> str:
        """
        Create a new manual collection. Returns a preview unless confirm=True.

        Smart (rule-based) collections are not supported — no rules can be
        supplied and none are sent. Omit handle to let Shopify derive it from
        the title. Refuses if a collection already exists at that handle.

        The new collection is NOT published to any sales channel; publishing
        is a separate operation.
        """
        if not title.strip():
            return "Provide a title for the collection."

        # Strip before use, not just for the emptiness check: the unstripped
        # value would otherwise be stored, previewed and logged with padding
        # while its handle was slugified without it.
        title = title.strip()
        caller_handle = handle.strip()

        # The handle is ALWAYS slugified here and ALWAYS sent, whether the
        # caller named it or it came from the title. Two consequences, both
        # deliberate (revised during review; originally an omitted handle was
        # left for Shopify to derive):
        #
        #  - The pre-read below and the mutation can never disagree about which
        #    handle is being claimed. A raw caller handle like "Grey Casualty"
        #    would otherwise make the pre-read look up a handle no collection
        #    can have, sailing past an existing 'grey-casualty'.
        #  - Nothing depends on reproducing Shopify's own derivation rules. Our
        #    slug is lossy where Shopify transliterates ("Über" gives "ber",
        #    not "uber"), so a predicted handle could never be trusted — but a
        #    handle we send is exact by construction. It also converts the
        #    silent "-1" suffix Shopify applies to auto-derived collisions into
        #    the explicit refusal the 2026-09-05 live probe recorded:
        #    userErrors [{field: [handle], message: "Handle has already been
        #    taken"}]. A surprise handle is no longer reachable.
        expected_handle = slugify_shopify_handle(caller_handle or title)
        if not expected_handle:
            # Names whichever source was actually slugified, so the message is
            # true both when the caller supplied the unusable value and when it
            # was derived from the title.
            return (
                f"Cannot form a collection handle from '{caller_handle or title}' — "
                f"handles may contain only letters, digits, hyphens and underscores."
            )

        # Decision 3: refuse a taken handle rather than let Shopify silently
        # suffix it and hand back a collection at a handle nobody expects.
        # Runs on the preview path too, so the collision is visible before
        # confirming. The refusal deliberately names only the handle — the
        # stored collection's title is merchant/import-authored text this tool
        # has no other reason to reflect (SEC-04).
        if ops.read_collection_by_handle(client, expected_handle):
            return (
                f"A collection already exists with handle '{expected_handle}'. "
                f"Choose a different handle, or use update_collection to edit it."
            )

        # None (not "") means "not provided" — see ops.create_collection.
        sanitized_description = sanitize_html(description) if description else None

        preview_lines = [
            "PREVIEW — Collection create",
            f"  Title  : {title}",
            f"  Handle : {expected_handle}"
            + ("" if caller_handle else " (derived from the title)"),
        ]
        if description:
            # The description is the caller's own input, not stored store
            # content, so it is rendered raw — fencing it would falsely label
            # the operator's own text as shopper-controlled (Story 10.63).
            preview_lines.append(
                f"  Description (full):\n{description}"
                + format_description_warning_block(html_safety_findings(description))
                + format_strip_block(html_strip_report(description, sanitized_description))
            )
        preview_lines.append(
            "  Publishing: the collection will not be published to any sales channel."
        )
        preview = "\n".join(preview_lines)

        # write_gate's done_text needs the mutation result, so the execute
        # callable captures it in the closure (per write_gate's docstring).
        created: dict[str, Any] = {}

        def _execute() -> dict[str, Any]:
            result = ops.create_collection(
                client,
                title=title,
                handle=expected_handle,
                description_html=sanitized_description,
            )
            created.update(result)
            return result

        def _created_node() -> dict[str, Any]:
            return (created.get("collectionCreate") or {}).get("collection") or {}

        def _done() -> str:
            node = _created_node()
            if not node:
                # No userErrors and no collection either. Whether the write
                # landed is genuinely unknown, so this must not report success
                # — but it is still logged above, because a create that DID
                # happen and went unrecorded is the worse failure in a server
                # with no delete tool.
                return (
                    "WARNING: the create reported no errors but returned no "
                    "collection, so it is unknown whether one was created. "
                    f"Check with get_collection(handle='{expected_handle}'). "
                    "The attempt is in the write log."
                )
            # Echoed unfenced because the handle is caller-derived — either the
            # caller's own slugified handle or one built from the caller's
            # title — never store-authored text. SEC-04's fencing rule is about
            # merchant/import-authored content, which this is not.
            actual_handle = node["handle"]
            lines = [
                "Done. Created collection.",
                f"  Title  : {title}",
                f"  Handle : {actual_handle}",
                # The id is the only durable way to find this collection again
                # — there is no delete tool, so manual cleanup in the admin
                # needs it. Every other output in this module reports one.
                f"  ID     : {from_gid(node['id'])}",
            ]
            if actual_handle != expected_handle:
                # Defence in depth, not an expected path. Because the handle is
                # always sent explicitly, the 2026-09-05 live probe showed a
                # collision is REFUSED with a userErrors entry rather than
                # silently suffixed — so reaching here means Shopify changed
                # its behaviour. Better to name the divergence than to report
                # success at a handle the caller never asked for.
                lines.append(
                    f"  NOTE: Shopify assigned '{actual_handle}', not the "
                    f"expected '{expected_handle}'."
                )
            lines.append("  Publishing: not published to any sales channel.")
            return "\n".join(lines)

        return write_gate(
            preview=preview,
            confirm=confirm,
            execute=_execute,
            mutation_key="collectionCreate",
            log_name="create_collection",
            # Callable, not an f-string: write_gate resolves this AFTER
            # execute(), so the audit line can name the handle Shopify actually
            # assigned. With no delete tool, a log naming a handle that does
            # not exist is a log that cannot find the collection it recorded.
            log_description=lambda: (
                f"handle={_created_node().get('handle') or expected_handle} | title={title}"
            ),
            done_text=_done,
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
                    f"\n  Job        : {numeric} (poll failed: {cap(str(poll_error))} — "
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
