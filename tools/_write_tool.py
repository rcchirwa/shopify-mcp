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

from tools._log import log_write
from tools._response import format_user_errors, with_confirm_hint


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

    done_text overrides the default f"Done. {preview}" return — use it when the
    tool's done string differs from its preview (e.g. "CONFIRMED — ..." prefix).
    Accepts a zero-arg callable when the done string depends on the mutation
    result (capture the result in the closure via the execute() callable).

    post_execute_check is called with the raw mutation result dict AFTER
    format_user_errors passes (no userErrors). If it returns a non-None string
    that string is returned as an error and log_write is NOT called. Use it for
    post-mutation response validation (e.g. missing IDs in the response payload).

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
        return f"Done. {preview}"
    return done_text() if callable(done_text) else done_text
