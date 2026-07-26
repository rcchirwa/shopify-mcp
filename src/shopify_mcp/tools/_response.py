"""Helpers for unwrapping and formatting Shopify mutation response payloads.

Covers the userErrors pattern shared across all write tools, and the
confirm-hint used by every write-tool preview branch.

Note: `_format_errors` / `_format_one_error` — the transport-level GQL error
formatters — live in shopify_client.py, not here. Those format
TransportQueryError payloads; these format GraphQL userErrors from mutation
response bodies.
"""

from typing import Any


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
    publications.py bulk flows, media.py stage-aware reporting) or format
    non-string field paths (products.py variant bulk update).

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
    Join a mutation's userErrors as 'field: message; field: message', or None if absent.

    Like `format_user_errors`, but without the canonical 'Error: ' prefix.
    Use when the output is embedded inside another sentence or report row
    where the prefix reads awkwardly — e.g. per-variant failure bullets in
    a bulk-op summary (rendered as `• {variant}: {error}`).

    If an error dict is missing `field` or `message`, each missing key renders
    as the literal string "None" (defensive: Shopify guarantees both keys, but
    unexpected shapes yield "None: None" rather than a KeyError).
    """
    errors = extract_user_errors(result, mutation_key, error_key=error_key)
    if not errors:
        return None
    return "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errors)


def format_user_errors(
    result: dict,
    mutation_key: str,
    *,
    error_key: str = "userErrors",
    prefix: str = "Error",
) -> str | None:
    """
    Extract and format a mutation's userErrors payload.

    Returns an 'Error: field: message; …' string if the mutation reported
    any userErrors, else None. Callers guard with `if err: return err`.

    - `error_key` overrides the default `userErrors` slot.
    - `prefix` customizes the leading token (e.g. 'Error creating price rule').
    """
    msgs = format_user_errors_joined(result, mutation_key, error_key=error_key)
    if msgs is None:
        return None
    return f"{prefix}: {msgs}"
