"""Shared confirm-gate / error-check / audit-log sequence for write tools.

Every single-mutation write tool has the same three-step boilerplate:
  1. confirm=False  → return preview with hint
  2. confirm=True   → execute mutation, check userErrors, call log_write
  3.                → return done string

`write_gate` centralises those steps so the omission (a missing log_write or
a skipped confirm check) becomes impossible in tools that use it.

Tools with custom error formatting (dotted field paths), per-item isolation,
multi-stage mutations, or job polling should NOT use write_gate — their control
flow is intentional and can't be collapsed without losing clarity.
"""

from collections.abc import Callable

from shopify_mcp.tools._log import log_write
from shopify_mcp.tools._response import format_user_errors, with_confirm_hint

_PREVIEW_MARKER = "PREVIEW — "
_CONFIRMED_MARKER = "CONFIRMED — "
_PARTIAL_MARKER = "PARTIAL — "
_FAILED_MARKER = "FAILED — "


def _confirmed_from_preview(preview: str) -> str:
    """Derive a confirmed-write message from a tool's preview string.

    Replaces the FIRST "PREVIEW — " header with "CONFIRMED — ", preserving
    whatever precedes it (e.g. an injection reminder prefixed ahead of the
    header) and leaving any later occurrence — e.g. inside caller-supplied
    data echoed back in the body — untouched.

    If `preview` has no "PREVIEW — " header at all, the marker is prefixed
    onto the whole string instead of silently falling back to an unlabelled
    string — a confirmed write must always read as confirmed.
    """
    if _PREVIEW_MARKER in preview:
        return preview.replace(_PREVIEW_MARKER, _CONFIRMED_MARKER, 1)
    return _CONFIRMED_MARKER + preview


def _outcome_header(heading: str, succeeded: int, failed: int) -> str:
    """Pick the CONFIRMED / PARTIAL / FAILED header for a batch write tool
    (a per-item mutation with its own done/failed lists) from a (succeeded,
    failed) pair the caller computes.

    That pair is keyed on the mutation(s) actually ATTEMPTED whenever
    anything at all resolved to a target — never on pre-mutation failures
    such as an unresolved channel name, which belong in the same "Failed:"
    block a caller renders below the header but are not themselves a
    rejected write. The one exception (round 2 of Story 9.24): when NOTHING
    resolved to a target at all, the caller falls back to counting those
    resolve failures as `failed`, so an all-unresolved batch doesn't read
    CONFIRMED over a Failed: block listing every requested item. See
    `publications._channel_write` / `set_product_publications` and
    `inventory.update_variant_inventory_tracking` for that fallback.

    That "nothing resolved" fallback is keyed on the RESOLVED set —
    `desired_nodes` in `set_product_publications`, `targets` in
    `_channel_write` and in `inventory.update_variant_inventory_tracking` —
    not on the REQUESTED names, and not on whether a mutation ran: at
    `set_product_publications`, an empty or entirely-unresolved
    `channel_names` still leaves a *declarative* empty
    (or unchanged) desired state, and a channel the caller never named can
    still get unpublished because it's no longer in that desired set — a
    real leg, attempted and landed independently of the resolve failure.
    When that happens, the fallback's resolve-failure count and that leg's
    real succeeded count land in the SAME (succeeded, failed) pair, so the
    header can read PARTIAL even though nothing Shopify actually rejected —
    see `test_set_unresolved_only_channel_still_removes_existing_publication_is_partial`
    (`test_publications.py`) for the pinned case.

    Story 9.24: `_channel_write`, `set_product_publications` and
    `update_variant_inventory_tracking` each built their own "CONFIRMED — "
    header unconditionally, so a write where every attempted item was
    rejected still read as CONFIRMED — the opposite hazard to 9.21/9.22's
    PREVIEW leak, and arguably worse, since nobody retries a write that
    reads as having landed. This is the one helper every such site now
    funnels its header through, so the rule can't drift between them.

    failed=0  → "CONFIRMED — {heading}", byte-identical to the pre-9.24
                unconditional header (this also covers the 0/0 case: an
                idempotent no-op attempted nothing, so nothing was rejected).
    succeeded=0 and failed>0 → "FAILED — {heading}" — nothing landed.
    Otherwise → "PARTIAL — {heading} (N succeeded, M failed)".
    """
    if failed == 0:
        return f"{_CONFIRMED_MARKER}{heading}"
    if succeeded == 0:
        return f"{_FAILED_MARKER}{heading}"
    return f"{_PARTIAL_MARKER}{heading} ({succeeded} succeeded, {failed} failed)"


def write_gate(
    *,
    preview: str,
    confirm: bool,
    execute: Callable[[], dict],
    mutation_key: str,
    log_name: str,
    log_description: str | Callable[[], str],
    error_key: str = "userErrors",
    done_text: str | Callable[[], str] | None = None,
    post_execute_check: Callable[[dict], str | None] | None = None,
) -> str:
    """Confirm gate, error check, and audit log for a single-mutation write tool.

    confirm=False → returns preview with the confirm hint appended; execute,
                    log_description (if callable), and log_write are not called.
    confirm=True  → calls execute(), checks userErrors via format_user_errors,
                    resolves log_description, calls log_write, returns done.

    log_description accepts a string or a zero-arg callable returning a string.
    Pass a callable when the description requires non-trivial construction the
    preview path shouldn't pay for.

    done_text overrides the default return, which derives a "CONFIRMED — ..."
    message from `preview` via `_confirmed_from_preview` (replacing its
    "PREVIEW — " header). Pass done_text when the tool's confirmed message
    needs more than that mechanical substitution (e.g. an extra line appended
    after execute() runs). Accepts a zero-arg callable when the done string
    depends on the mutation result (capture the result in the closure via the
    execute() callable).

    post_execute_check is called with the raw mutation result dict AFTER
    format_user_errors passes (no userErrors). The dict is the full, unmodified
    value returned by execute() — callers must navigate to the mutation key
    themselves (e.g. result.get("mutationName", {})). If it returns a non-None
    string that string is returned as an error and log_write is NOT called. Use it
    for post-mutation response validation (e.g. missing IDs in the response payload).

    Tools that short-circuit before the mutation (no-op fast paths, empty-batch
    guards) must return early before calling write_gate so the mutation is never
    dispatched and log_write is not called.
    """
    if not confirm:
        return with_confirm_hint(preview)
    result = execute()
    err = format_user_errors(result, mutation_key, error_key=error_key)
    if err:
        return err
    if post_execute_check is not None:
        check_err = post_execute_check(result)
        if check_err is not None:
            return check_err
    desc = log_description() if callable(log_description) else log_description
    log_write(log_name, desc)
    if done_text is None:
        return _confirmed_from_preview(preview)
    return done_text() if callable(done_text) else done_text
