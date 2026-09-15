"""
Discount tools — read and create discount codes.

Thin MCP-tool surface over ``shopify.operations.discounts``: this module keeps
param coercion, the ``DiscountCodeBasicInput`` assembly, the preview/confirm
flow, and output formatting; the GraphQL strings live in
``shopify.queries.discounts`` and the data access in
``shopify.operations.discounts`` (Story 10.27 / A5).

create_discount_code requires confirm=True.
"""

from datetime import UTC, date, datetime, time
from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_mcp.client import ShopifyClient
from shopify_mcp.shopify.operations import discounts as ops
from shopify_mcp.shopify.queries.discounts import (
    CREATE_DISCOUNT_CODE_BASIC,
    GET_CODE_DISCOUNTS,
)
from shopify_mcp.tools._gid import from_gid
from shopify_mcp.tools._log import log_write
from shopify_mcp.tools._response import (
    extract_user_errors,
    format_path_user_errors,
    with_confirm_hint,
)

# Shopify rejects a 0% or negative discount, and a >100% value would zero out
# (or overpay) a line item — bound client-side rather than let a nonsensical
# code preview as legitimate (SEC-07).
DISCOUNT_PCT_MIN = 0
DISCOUNT_PCT_MAX = 100

# The wire format Shopify accepts for DiscountCodeBasicInput's startsAt/endsAt.
_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"
# A bare calendar date is read as the last second of that day — see
# _normalize_ends_at for why.
_END_OF_DAY = time(23, 59, 59)


def _normalize_ends_at(value: str, starts_at: datetime) -> tuple[str, str]:
    """Parse a caller-supplied expiry into Shopify's ISO-8601 UTC wire format.

    Returns ``(iso, "")`` on success and ``("", error)`` on failure, matching
    this module's convention of returning an error string rather than raising.

    Story 9.15 introduced the first caller-supplied date in this codebase, so
    the judgement calls are recorded here rather than left implicit:

    * A **bare calendar date means the END of that day.** An operator who says
      "expires 2026-09-19" means the 19th is the last day the code works, not
      that it dies as the 19th begins. Reading it as midnight would silently
      cut a promotion a day short — the same shape of failure this story
      exists to prevent, just moved from "never expires" to "expired early".
    * A naive timestamp is read as UTC rather than refused.
    * The result must fall *strictly* after ``starts_at``, since an equal or
      earlier value creates a code already expired the moment it exists.
    """
    try:
        # date.fromisoformat rejects anything carrying a time component, which
        # makes it a clean discriminator for "date only" — no string sniffing.
        parsed = datetime.combine(date.fromisoformat(value), _END_OF_DAY, tzinfo=UTC)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            # TypeError as well as ValueError: fromisoformat raises TypeError on
            # a non-str, and this function's contract is to return an error
            # string rather than let one shape of bad input raise.
            return "", (
                f"Error: ends_at must be an ISO-8601 date or timestamp "
                f"(e.g. 2026-09-20 or 2026-09-20T23:59:59Z) — got {value!r}."
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
    # Truncate to the wire format's resolution BEFORE comparing. Flooring only
    # starts_at is not enough: a value 0.9s after a whole-second start compares
    # as later while serializing to the same second, so the guard would pass and
    # Shopify would receive endsAt == startsAt.
    parsed = parsed.replace(microsecond=0)
    if parsed <= starts_at:
        return "", (
            f"Error: ends_at must be after the start "
            f"({starts_at.strftime(_ISO_Z)}) — got {parsed.strftime(_ISO_Z)}."
        )
    return parsed.strftime(_ISO_Z), ""


# The GraphQL strings now live in shopify.queries.discounts. They are re-exported
# here so existing callers/tests (`from tools.discounts import GET_CODE_DISCOUNTS`)
# keep resolving to the same objects the operations layer executes.
__all__ = [
    "CREATE_DISCOUNT_CODE_BASIC",
    "GET_CODE_DISCOUNTS",
    "register",
]


def register(server: FastMCP, client: ShopifyClient) -> None:

    @server.tool()
    def get_discount_codes() -> str:
        """List discount codes for the store."""
        nodes = ops.read_code_discounts(client)
        if not nodes:
            return "No discount codes found."

        lines = [f"Discount codes ({len(nodes)} found):\n"]
        for node in nodes:
            discount = node.get("discount") or {}
            codes_conn = discount.get("codes") or {}
            # `or []`, not `.get("nodes", [])`: a permissions-trimmed / shape-
            # drifted response can return "nodes": null, which the dict-default
            # form would not catch.
            codes = [c["code"] for c in (codes_conn.get("nodes") or []) if c.get("code")]
            codes_str = ", ".join(codes) if codes else "(no code)"
            # Only note truncation when there's a real (non-empty) list to
            # truncate — otherwise a shape-drifted "nodes: null, hasNextPage:
            # true" response would render the self-contradictory
            # "(no code) (+more not shown)".
            if codes and (codes_conn.get("pageInfo") or {}).get("hasNextPage"):
                codes_str += " (+more not shown)"
            value = (discount.get("customerGets") or {}).get("value") or {}
            value_type = value.get("__typename")
            if value_type == "DiscountPercentage":
                # `.get()`, not a bare subscript: a permissions-trimmed response
                # could report the union member's __typename without every leaf
                # field resolving.
                value_line = f"{(value.get('percentage') or 0) * 100:g}% off"
            elif value_type == "DiscountAmount":
                amount = ((value.get("amount") or {}).get("amount")) or "N/A"
                value_line = f"${amount} off"
            else:
                value_line = discount.get("__typename", "")
            lines.append(
                f"  [{from_gid(node['id'])}] {discount.get('title', '')}\n"
                f"    Codes: {codes_str} | {value_line} | Status: {discount.get('status', '')} | "
                f"Usage limit: {discount.get('usageLimit') or 'unlimited'} | "
                f"Ends: {discount.get('endsAt') or 'no expiry'}"
            )
        return "\n".join(lines)

    @server.tool()
    def create_discount_code(
        title: str,
        code: str,
        percentage_off: float,
        usage_limit: int = 0,
        ends_at: str = "",
        confirm: bool = False,
    ) -> str:
        """
        Create a new percentage-off discount code.
        percentage_off: e.g. 20 = 20% off. Must be > 0 and <= 100.
        usage_limit: 0 = unlimited.
        ends_at: optional expiry as an ISO-8601 date or timestamp (e.g.
          2026-09-20 or 2026-09-20T23:59:59Z); a value with no timezone is read
          as UTC. Omit for a code that never expires. Must be after now.
        Returns a preview unless confirm=True.
        """
        if not (DISCOUNT_PCT_MIN < percentage_off <= DISCOUNT_PCT_MAX):
            return (
                f"Error: percentage_off must be > {DISCOUNT_PCT_MIN} and "
                f"<= {DISCOUNT_PCT_MAX} (got {percentage_off})."
            )

        # Stamped once and reused for both the expiry comparison and the
        # payload: calling now() twice could compare against one instant and
        # send another. Floored to whole seconds so the guard compares exactly
        # what the wire format carries — at microsecond precision a sub-second
        # window passes the check and then serializes to endsAt == startsAt.
        starts_at = datetime.now(UTC).replace(microsecond=0)
        ends_at_iso = ""
        if ends_at:
            ends_at_iso, error = _normalize_ends_at(ends_at, starts_at)
            if error:
                return error

        preview = (
            f"PREVIEW — New discount code\n"
            f"  Title         : {title}\n"
            f"  Code          : {code}\n"
            f"  Discount      : {percentage_off}% off\n"
            f"  Usage limit   : {'unlimited' if usage_limit == 0 else usage_limit}\n"
            f"  Ends          : {ends_at_iso or 'no expiry'}"
        )

        if not confirm:
            return with_confirm_hint(preview)

        discount_input: dict[str, Any] = {
            "title": title,
            "code": code,
            "startsAt": starts_at.strftime(_ISO_Z),
            # Nullable in the schema, but confirmed live (2026-09-14) that
            # discountCodeBasicCreate rejects a missing `context` with "Context
            # can't be blank" — {all: ALL} is the buyer-selection equivalent of
            # the old PriceRuleInput.customerSelection.forAllCustomers.
            "context": {"all": "ALL"},
            "customerGets": {
                "value": {"percentage": percentage_off / 100},
                "items": {"all": True},
            },
        }
        if usage_limit > 0:
            discount_input["usageLimit"] = usage_limit
        # Conditional, mirroring usageLimit above: the no-expiry path must send
        # no endsAt key at all rather than an explicit null, preserving the
        # pre-9.15 payload byte-for-byte for callers that don't pass one.
        if ends_at_iso:
            discount_input["endsAt"] = ends_at_iso

        result = ops.create_discount_code_basic(client, discount_input)
        errors = extract_user_errors(result, "discountCodeBasicCreate")
        if errors:
            return f"Error creating discount code: {format_path_user_errors(errors)}"

        # codeDiscountNode is None when the mutation shape-drifts or userErrors
        # are empty but the server-side commit still failed — guard with `or {}`
        # (same pattern as tools/inventory.py `.get("inventoryItem") or {}`).
        node_id = ((result.get("discountCodeBasicCreate") or {}).get("codeDiscountNode") or {}).get(
            "id"
        )
        if not node_id:
            return "Error: discount code created but no ID returned."

        # SEC-12: the discount code is masked in the durable audit log. The
        # node id below already identifies the discount, and the code is
        # recoverable from Shopify — so plaintext buys no audit value while
        # leaving a secret-shaped string in a local file.
        log_write(
            "create_discount_code",
            f"title={title} code=*** percentage_off={percentage_off}% usage_limit={usage_limit}",
        )
        return f"Done. Discount id={from_gid(node_id)} created.\n{preview}"
