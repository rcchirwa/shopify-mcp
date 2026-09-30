"""Helpers for unwrapping and formatting Shopify mutation response payloads.

Covers the userErrors pattern shared across all write tools, and the
confirm-hint used by every write-tool preview branch.

Note: `_format_errors` / `_format_one_error` — the transport-level GQL error
formatters — live in shopify_mcp/client.py, not here. Those format
TransportQueryError payloads; these format GraphQL userErrors from mutation
response bodies.
"""

from typing import Any

from shopify_mcp.tools._scrub import cap, sanitize_control_chars
from shopify_mcp.tools._untrusted import wrap_reflected


def poll_failed_note(error: object) -> str:
    """Render the shared 'poll failed' note used by every write tool whose
    mutation succeeded but the follow-up job poll did not.

    Bounds `error` through `cap` (SEC-27) before embedding it — was
    hand-inlined at three call sites (`tools/collections.py`,
    `tools/media/_reorder.py`, `tools/media/_upload.py`); Story 10.89 code
    review (F7) moved it here so all three stay byte-identical.
    """
    return f"(poll failed: {cap(str(error))} — underlying write succeeded, check server-side for completion)"


def format_field_path(error: dict[str, Any]) -> str:
    """
    Join a UserError's list-typed `field` into a dotted path.

    Several UserError types declare `field` as `[String!]` — a path addressing
    a nested input value, e.g. `["basicCodeDiscount", "code"]` or
    `["variants", "0", "price"]` — rather than a scalar string. `str(field)`
    on the raw list reads poorly in an error head, so every mutation whose
    input addresses a nested value (`metafieldsSet`, `productOptionUpdate`,
    `discountCodeBasicCreate`, `productVariantsBulkUpdate`, …) renders it
    through this helper instead — as does `format_user_errors`, via
    `format_path_user_errors` (Story 10.94).

    Missing, null, and empty `field` all yield `""` — the helper stays
    placeholder-free so callers can pick their own ("(no field)", "(unknown)").

    Segments are `str()`-coerced: the schema types them as strings, so this
    only guards against an unexpected int rather than raising.

    Assumes `field` is a sequence of segments, per the `[String!]` schema. A
    caller that can also receive a *scalar* string must guard with
    `isinstance(field, list)` before calling — iterating a string would
    join it character-by-character. `publications.py`'s `_map_user_error`
    is the one such caller.
    """
    return ".".join(str(f) for f in (error.get("field") or []))


def format_path_user_errors(errors: list[dict[str, Any]]) -> str:
    """
    Join already-extracted UserErrors as 'field.path: message; …'.

    The body of `format_user_errors_joined`, and the shared home for a
    formatter that was hand-inlined at seven call sites. Takes the
    error list directly rather than `(result, mutation_key)` because most
    callers format a *filtered* subset — `ACCESS_DENIED` entries, the
    non-idempotent remainder of a delete — for which no mutation slot exists.

    A missing or empty `field` renders as "(no field)"; a missing `message`
    renders as empty. Returns "" for an empty list — callers guard on the
    error list being non-empty before formatting, so there is no None signal.

    The joined string is scrubbed as a whole (Story 10.75, SEC-24-remaining-
    sites): its CR/LF escaped, path segments included, then bounded. Since
    Story 10.69 (SEC-04-errors) the report is also fenced as untrusted, as one
    value: it is Shopify's text, and Shopify echoes input into it.
    `wrap_reflected` does the bounding, so the fenced string, delimiters
    included, is at most `REFLECT_MAX_LEN`. Sanitizing first keeps the escapes
    inside the bound. Every userError route inherits this —
    `format_user_errors*`, `write_gate`, and the direct callers — and each
    caller that renders it adds the reminder with `with_reminder()`.
    """
    if not errors:
        return ""
    return wrap_reflected(
        "",
        sanitize_control_chars(
            "; ".join(
                f"{format_field_path(e) or '(no field)'}: {e.get('message', '')}" for e in errors
            )
        ),
    )


def with_confirm_hint(preview: str) -> str:
    """Append the confirm hint used by every write-tool preview branch."""
    return preview + "\n\nTo apply, call again with confirm=True."


def extract_user_errors(
    result: dict,
    mutation_key: str,
    *,
    error_key: str = "userErrors",
) -> list[dict[str, Any]]:
    """
    Pull the userErrors list out of a mutation response, or [] if absent/null.

    Shared by every tool that needs to inspect userErrors — including callers
    that can't use `format_user_errors` because they iterate each error (e.g.
    publications.py bulk flows, media.py stage-aware reporting) or filter the
    list before formatting it through `format_path_user_errors`.

    - `error_key` overrides the default `userErrors` slot; `priceRuleCreate`
      returns `priceRuleUserErrors` instead.
    """
    return (result.get(mutation_key) or {}).get(error_key) or []


def format_user_errors_joined(
    result: dict,
    mutation_key: str,
    *,
    error_key: str = "userErrors",
) -> str | None:
    """
    Join a mutation's userErrors as 'field.path: message; …', or None if absent.

    Like `format_user_errors`, but without the canonical 'Error: ' prefix.
    Use when the output is embedded inside another sentence or report row
    where the prefix reads awkwardly — e.g. per-variant failure bullets in
    a bulk-op summary (rendered as `• {variant}: {error}`).

    `field` is `[String!]` in the Admin schema, so each error renders through
    `format_path_user_errors`: `["input", "variants", "0", "price"]` becomes
    `input.variants.0.price`, a missing/null/empty `field` renders as
    "(no field)", and a missing `message` renders as empty (Story 10.94 —
    this used to interpolate `field` raw, showing operators a list repr).
    """
    errors = extract_user_errors(result, mutation_key, error_key=error_key)
    if not errors:
        return None
    return format_path_user_errors(errors)


def format_user_errors(
    result: dict,
    mutation_key: str,
    *,
    error_key: str = "userErrors",
    prefix: str = "Error",
) -> str | None:
    """
    Extract and format a mutation's userErrors payload.

    Returns an 'Error: field.path: message; …' string if the mutation reported
    any userErrors, else None. Callers guard with `if err: return err`.

    - `error_key` overrides the default `userErrors` slot.
    - `prefix` customizes the leading token (e.g. 'Error creating price rule').
    """
    msgs = format_user_errors_joined(result, mutation_key, error_key=error_key)
    if msgs is None:
        return None
    return f"{prefix}: {msgs}"
