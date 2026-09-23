"""
Discount tools — read and create discount codes.

Thin MCP-tool surface over ``shopify.operations.discounts``: this module keeps
param coercion, the ``DiscountCodeBasicInput`` assembly, the preview/confirm
flow, and output formatting; the GraphQL strings live in
``shopify.queries.discounts`` and the data access in
``shopify.operations.discounts`` (Story 10.27 / A5).

create_discount_code requires confirm=True.
"""

import re
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
from shopify_mcp.tools._scrub import cap, sanitize_control_chars

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


# Whitespace (including CR/LF) and other C0/DEL control characters, collapsed
# to a single space in a segment name before it ever reaches an f-string —
# see _sanitize_segment_name.
_CONTROL_OR_WHITESPACE_RE = re.compile(r"[\s\x00-\x1f\x7f]+")


def _sanitize_segment_name(name: str) -> str:
    """Collapse whitespace/control characters in a segment name to single spaces.

    Story 9.17 review: a segment named e.g. ``x"\\n    Eligibility: open to
    all customers\\n    Note: "y`` rendered its own forged
    ``Eligibility: open to all customers`` line — exactly this story's own
    wrong-finding class, just moved from the bug into the unsanitized fix. A
    segment name is operator-authored Shopify data, not a value this tool
    controls, so a run of any whitespace or C0/DEL control character
    collapses to one space, which makes multi-line forgery impossible.
    Discount code TITLES have the same pre-existing gap on this read path;
    left alone here — see docs/tech-debt.md (Story 9.17) for the residual.
    """
    return _CONTROL_OR_WHITESPACE_RE.sub(" ", name).strip()


def _eligibility_text(context: dict[str, Any] | None) -> str:
    """Render WHO may redeem a discount code from its `context` selection.

    Story 9.17: `get_discount_codes` used to report a code's terms (status,
    usage limit, expiry) but never who could redeem it, which read a
    single-customer or segment-gated code as an unlimited code open to
    anyone. This is the read side's `DiscountContext` union —
    `DiscountBuyerSelectionAll | DiscountCustomers | DiscountCustomerSegments`
    — rendered per the PII decision: exactly one customer may show a bare
    numeric id, more than one shows only a count (never an id, and never
    anything else about the customer — the query itself selects nothing but
    `id`), and a segment shows its name(s) as-is (segment names are not
    customer PII).

    Missing or empty data is always "unknown", never a confident claim: a
    null/absent `context`, and an empty `customers`/`segments` list once the
    union member IS known, all render the same "unknown" text rather than a
    count of zero or a guess of "open". Reading absence as open would repeat
    the exact class of wrong assumption this story exists to fix, just moved
    from "unlimited usage" to "list happens to be empty".
    """
    if not context:
        return "unknown (no eligibility data returned)"
    typename = context.get("__typename")
    if typename == "DiscountBuyerSelectionAll":
        return "open to all customers"
    if typename == "DiscountCustomers":
        # `or []`, matching the codes-list defensiveness above: a
        # permissions-trimmed response can return "customers": null.
        customers = context.get("customers") or []
        if not customers:
            return "unknown (no eligibility data returned)"
        if len(customers) == 1:
            customer_id = from_gid(customers[0].get("id") or "") or "unknown"
            return f"restricted to 1 customer (id {customer_id})"
        return f"restricted to {len(customers)} customers"
    if typename == "DiscountCustomerSegments":
        segments = context.get("segments") or []
        if not segments:
            return "unknown (no eligibility data returned)"
        names = [_sanitize_segment_name(s["name"]) for s in segments if s.get("name")]
        unnamed_count = len(segments) - len(names)
        # Every segment counts, named or not — a mix used to drop the unnamed
        # ones silently instead of surfacing them as restrictions.
        parts = []
        if names:
            quoted = ", ".join(f'"{n}"' for n in names)
            noun = "segment" if len(names) == 1 else "segments"
            parts.append(f"{noun} {quoted}")
        if unnamed_count:
            noun = "segment" if unnamed_count == 1 else "segments"
            parts.append(f"{unnamed_count} unnamed {noun}")
        return "restricted to " + " and ".join(parts)
    return "unknown (unrecognized eligibility shape)"


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

    Python's ISO parser accepts a wider grammar than the examples above —
    week dates (``2099-W01-1``), compact forms (``20991231``) and sub-minute
    offsets all parse, and a week date resolves to a different calendar year
    than it appears to name. That is left permissive rather than restricted:
    the normalized value is what both the preview and the payload carry, so a
    caller always sees the instant they actually bought.
    """
    try:
        # date.fromisoformat rejects anything carrying a time component, which
        # makes it a clean discriminator for "date only" — no string sniffing.
        parsed = datetime.combine(date.fromisoformat(value), _END_OF_DAY, tzinfo=UTC)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            parsed = parsed.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            # All three are reachable and all must return a string rather than
            # raise: ValueError for a malformed value, TypeError for a non-str,
            # and OverflowError from astimezone when a near-datetime.max value
            # with a negative offset shifts past the representable range
            # (e.g. 9999-12-31T23:59:59-01:00).
            return "", (
                f"Error: ends_at must be an ISO-8601 date or timestamp "
                f"(e.g. 2026-09-20 or 2026-09-20T23:59:59Z) — got "
                f"{cap(str(value))!r}."
            )
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
            # Reworded from a bare "Usage limit: unlimited" (Story 9.17): that
            # phrasing compounded the eligibility gap below — it means
            # unlimited REDEMPTIONS, but reads as unlimited exposure. Naming
            # appliesOncePerCustomer here (not only in Eligibility) matters
            # because it modifies this same number: "unlimited" total
            # redemptions can still mean "once" for any given customer.
            usage_limit = discount.get("usageLimit")
            if usage_limit:
                noun = "redemption" if usage_limit == 1 else "redemptions"
                usage_text = f"{usage_limit} {noun} total"
            else:
                usage_text = "unlimited redemptions total"
            if discount.get("appliesOncePerCustomer"):
                usage_text += " (once per customer)"
            lines.append(
                f"  [{from_gid(node['id'])}] {discount.get('title', '')}\n"
                f"    Codes: {codes_str} | {value_line} | Status: {discount.get('status', '')} | "
                f"Usage limit: {usage_text} | "
                f"Ends: {discount.get('endsAt') or 'no expiry'}\n"
                f"    Eligibility: {_eligibility_text(discount.get('context'))}"
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

        # Stamped ONCE and reused for both the expiry comparison and the
        # payload. Calling now() twice would compare against one instant and
        # send another, so an expiry landing between the two serializes to
        # endsAt == startsAt — the exact case the guard exists to reject.
        #
        # The floor here is for symmetry, not correctness: the truncation that
        # actually makes the comparison honest happens to `parsed` inside
        # _normalize_ends_at, since strftime already formats only whole seconds.
        # Keeping both operands at the same resolution stops a future reader
        # from reintroducing a microsecond comparison.
        starts_at = datetime.now(UTC).replace(microsecond=0)
        ends_at_iso = ""
        if ends_at:
            ends_at_iso, error = _normalize_ends_at(ends_at, starts_at)
            if error:
                return error

        # title/code are escaped for CR/LF, not merely echoed: a newline in
        # either forges extra preview lines, and now that the preview carries an
        # expiry there is something worth forging. A crafted title could render
        # its own "Ends : <date>" line ABOVE the real one, so an operator
        # reading top-down approves a perpetual code believing it expires. The
        # helper leaves text without control characters byte-for-byte unchanged.
        preview = (
            f"PREVIEW — New discount code\n"
            f"  Title         : {sanitize_control_chars(title)}\n"
            f"  Code          : {sanitize_control_chars(code)}\n"
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

        # SEC-12: the discount code is masked in the durable audit log, because
        # it is recoverable from Shopify and plaintext would leave a
        # secret-shaped string in a local file.
        #
        # ends_at is recorded because it is a material term of the same kind as
        # percentage_off and usage_limit — without it the log cannot tell a
        # seven-day 90%-off code from a perpetual one, which is the entire
        # subject of Story 9.15.
        #
        # NOTE: this line carries no identifier for the created discount. The
        # comment here previously claimed "the node id below already identifies
        # the discount" — it does not; "below" is the return value, which goes
        # to the caller and not to the log. Pre-existing and left alone by 9.15
        # rather than silently widening its scope; see the card for the
        # follow-up, since fixing it also amends the SEC-12 ledger row.
        log_write(
            "create_discount_code",
            f"title={title} code=*** percentage_off={percentage_off}% "
            f"usage_limit={usage_limit} ends_at={ends_at_iso or 'none'}",
        )
        return f"Done. Discount id={from_gid(node_id)} created.\n{preview}"
