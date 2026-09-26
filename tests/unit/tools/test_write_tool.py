"""
Offline unit tests for tools/_write_tool.py — write_gate() helper.

write_gate() is a pure control-flow function; tests exercise it directly
without going through a registered MCP tool or FakeClient.
"""

import pytest

import shopify_mcp.tools._write_tool as _wt
from shopify_mcp.client import TransientShopifyError
from shopify_mcp.tools._response import with_confirm_hint


def _ok(mutation_key: str = "productUpdate") -> dict:
    return {mutation_key: {"userErrors": []}}


def _err(mutation_key: str = "productUpdate", field: str = "title", msg: str = "too short") -> dict:
    return {mutation_key: {"userErrors": [{"field": field, "message": msg}]}}


# ---------- preview (confirm=False) ----------


def test_preview_returns_hint_without_calling_execute() -> None:
    called: list[int] = []

    def _execute() -> dict:
        called.append(1)
        return {}

    out = _wt.write_gate(
        preview="PREVIEW — something",
        confirm=False,
        execute=_execute,
        mutation_key="productUpdate",
        log_name="test_tool",
        log_description="desc",
    )
    assert out == with_confirm_hint("PREVIEW — something")
    assert called == [], "execute must not be called on preview path"


# ---------- confirm=True success ----------


def test_confirm_returns_confirmed_header_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story 9.21: the default done text must read as CONFIRMED, never as
    "Done. PREVIEW — …" — that string reads to an operator as an unapplied
    preview and caused a confirmed write to be redone by hand."""
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(_wt, "log_write", lambda name, msg: logged.append((name, msg)))

    out = _wt.write_gate(
        preview="PREVIEW — update title",
        confirm=True,
        execute=lambda: _ok(),
        mutation_key="productUpdate",
        log_name="update_product_title",
        log_description="id=1 | 'A' → 'B'",
    )

    assert out == "CONFIRMED — update title"
    assert "PREVIEW" not in out
    assert logged == [("update_product_title", "id=1 | 'A' → 'B'")]


def test_confirmed_from_preview_preserves_prefix_before_preview_marker() -> None:
    """A preview can carry a prefix before its "PREVIEW — " header — e.g.
    update_collection's preview is wrapped by with_reminder(), which prefixes
    an injection reminder ahead of the header when it applies. The derivation
    must replace only the header, leaving the prefix intact. (Only the four
    tools that omit done_text — update_product_title, update_collection,
    update_inventory, delete_webhook — reach this helper; the other two sites
    with a preview-prefix pattern, update_product_description and
    register_webhook, do the identical substitution inline.)"""
    out = _wt._confirmed_from_preview("⚠ some warning\nPREVIEW — update title")
    assert out == "⚠ some warning\nCONFIRMED — update title"


def test_confirmed_from_preview_labels_output_when_no_preview_marker_present() -> None:
    """A preview with no "PREVIEW — " header at all must still read as
    confirmed rather than silently falling back to an unlabelled string —
    the whole point of this helper is that a confirmed write is never
    ambiguous about whether it landed."""
    out = _wt._confirmed_from_preview("no marker here")
    assert out == "CONFIRMED — no marker here"
    assert "PREVIEW" not in out


def test_confirmed_from_preview_replaces_first_occurrence_only() -> None:
    """Adversarial (Story 9.21 verifier finding): a preview's BODY (built from
    merchant/caller data after the tool's own header) can itself contain the
    literal string "PREVIEW — ...". Only the tool's own header — the FIRST
    occurrence — may become "CONFIRMED — "; a "replace all" implementation
    would also rewrite the embedded data, and a "replace last" implementation
    would rewrite the embedded data INSTEAD of the header. Both falsify what
    the operator is shown."""
    out = _wt._confirmed_from_preview(
        "PREVIEW — update title\n  New title  : PREVIEW — not applied, redo by hand"
    )
    assert out == "CONFIRMED — update title\n  New title  : PREVIEW — not applied, redo by hand"


def test_confirm_returns_done_text_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: None)

    out = _wt.write_gate(
        preview="PREVIEW — update status",
        confirm=True,
        execute=lambda: _ok(),
        mutation_key="productUpdate",
        log_name="update_product_status",
        log_description="id=1 | ACTIVE → DRAFT",
        done_text="CONFIRMED — Product status updated\n  New status : DRAFT",
    )

    assert out == "CONFIRMED — Product status updated\n  New status : DRAFT"


# ---------- callable log_description ----------


def test_callable_log_description_is_invoked_only_on_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(_wt, "log_write", lambda name, msg: logged.append((name, msg)))
    desc_calls: list[int] = []

    def _desc() -> str:
        desc_calls.append(1)
        return "id=1 | computed lazily"

    # preview path — callable must not be invoked
    out_preview = _wt.write_gate(
        preview="PREVIEW — x",
        confirm=False,
        execute=lambda: _ok(),
        mutation_key="productUpdate",
        log_name="t",
        log_description=_desc,
    )
    assert out_preview.startswith("PREVIEW — x")
    assert desc_calls == []
    assert logged == []

    # confirm path — callable invoked exactly once, result forwarded to log_write
    out_done = _wt.write_gate(
        preview="PREVIEW — x",
        confirm=True,
        execute=lambda: _ok(),
        mutation_key="productUpdate",
        log_name="t",
        log_description=_desc,
    )
    assert out_done == "CONFIRMED — x"
    assert desc_calls == [1]
    assert logged == [("t", "id=1 | computed lazily")]


def test_callable_log_description_not_invoked_on_user_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """userErrors short-circuit before log_description is resolved."""
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: None)
    desc_calls: list[int] = []

    def _desc() -> str:
        desc_calls.append(1)
        return "should not appear"

    out = _wt.write_gate(
        preview="PREVIEW — x",
        confirm=True,
        execute=lambda: _err(),
        mutation_key="productUpdate",
        log_name="t",
        log_description=_desc,
    )
    assert out.startswith("Error:")
    assert desc_calls == [], "log_description callable must not run when userErrors are present"


# ---------- userErrors ----------


def test_confirm_user_errors_returns_error_without_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[int] = []
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: logged.append(1))

    out = _wt.write_gate(
        preview="PREVIEW — update title",
        confirm=True,
        execute=lambda: _err(field="title", msg="must be unique"),
        mutation_key="productUpdate",
        log_name="update_product_title",
        log_description="id=1 | 'A' → 'A'",
    )

    assert out.startswith("Error:") and "must be unique" in out
    assert logged == [], "log_write must NOT be called when userErrors are present"


def test_custom_error_key_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: None)

    result = {"priceRuleCreate": {"priceRuleUserErrors": [{"field": "value", "message": "bad"}]}}

    out = _wt.write_gate(
        preview="PREVIEW — create discount",
        confirm=True,
        execute=lambda: result,
        mutation_key="priceRuleCreate",
        log_name="create_discount_code",
        log_description="code=SALE10",
        error_key="priceRuleUserErrors",
    )

    assert out.startswith("Error:") and "bad" in out


# ---------- done_text callable variant ----------


def test_done_text_callable_invoked_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: None)
    calls: list[int] = []

    def _done() -> str:
        calls.append(1)
        return "DONE — custom callable text"

    out = _wt.write_gate(
        preview="PREVIEW — x",
        confirm=True,
        execute=lambda: _ok(),
        mutation_key="productUpdate",
        log_name="t",
        log_description="desc",
        done_text=_done,
    )
    assert out == "DONE — custom callable text"
    assert calls == [1]


def test_done_text_callable_not_invoked_on_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _done() -> str:
        calls.append(1)
        return "should not be called"

    out = _wt.write_gate(
        preview="PREVIEW — x",
        confirm=False,
        execute=lambda: _ok(),
        mutation_key="productUpdate",
        log_name="t",
        log_description="desc",
        done_text=_done,
    )
    assert out.startswith("PREVIEW — x")
    assert calls == []


def test_done_text_callable_not_invoked_on_user_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: None)
    calls: list[int] = []

    def _done() -> str:
        calls.append(1)
        return "should not be called"

    out = _wt.write_gate(
        preview="PREVIEW — x",
        confirm=True,
        execute=lambda: _err(),
        mutation_key="productUpdate",
        log_name="t",
        log_description="desc",
        done_text=_done,
    )
    assert out.startswith("Error:")
    assert calls == []


# ---------- post_execute_check ----------


def test_post_execute_check_none_does_not_block_success(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(_wt, "log_write", lambda name, msg: logged.append((name, msg)))
    received: list[dict] = []

    def _check(result: dict) -> str | None:
        received.append(result)
        return None

    out = _wt.write_gate(
        preview="PREVIEW — y",
        confirm=True,
        execute=lambda: _ok(),
        mutation_key="productUpdate",
        log_name="tool_y",
        log_description="desc",
        post_execute_check=_check,
    )
    assert out == "CONFIRMED — y"
    assert logged == [("tool_y", "desc")]
    assert received == [_ok()]


def test_post_execute_check_error_blocks_log_and_done(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[int] = []
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: logged.append(1))

    def _check(result: dict) -> str | None:
        return "Error: missing required field in response"

    out = _wt.write_gate(
        preview="PREVIEW — y",
        confirm=True,
        execute=lambda: _ok(),
        mutation_key="productUpdate",
        log_name="tool_y",
        log_description="desc",
        post_execute_check=_check,
    )
    assert out == "Error: missing required field in response"
    assert logged == [], "log_write must NOT be called when post_execute_check returns an error"


def test_post_execute_check_skipped_on_user_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: None)
    check_calls: list[int] = []

    def _check(result: dict) -> str | None:
        check_calls.append(1)
        return None

    out = _wt.write_gate(
        preview="PREVIEW — y",
        confirm=True,
        execute=lambda: _err(),
        mutation_key="productUpdate",
        log_name="t",
        log_description="desc",
        post_execute_check=_check,
    )
    assert out.startswith("Error:")
    assert check_calls == [], "post_execute_check must not run when userErrors are present"


def test_post_execute_check_skipped_on_preview() -> None:
    check_calls: list[int] = []

    def _check(result: dict) -> str | None:
        check_calls.append(1)
        return None

    _wt.write_gate(
        preview="PREVIEW — y",
        confirm=False,
        execute=lambda: _ok(),
        mutation_key="productUpdate",
        log_name="t",
        log_description="desc",
        post_execute_check=_check,
    )
    assert check_calls == []


# ---------- exception propagation ----------


def test_transient_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: None)

    def _raise() -> dict:
        raise TransientShopifyError("throttled")

    with pytest.raises(TransientShopifyError, match="throttled"):
        _wt.write_gate(
            preview="PREVIEW — something",
            confirm=True,
            execute=_raise,
            mutation_key="productUpdate",
            log_name="test_tool",
            log_description="desc",
        )


# ---------- _outcome_header ----------
#
# Batch write tools with per-item outcomes (a done list next to a failed
# list) funnel their CONFIRMED / PARTIAL / FAILED header through this one
# helper, keyed on (succeeded, failed) mutation counts, so the choice can't
# drift between call sites (publications._channel_write,
# publications.set_product_publications,
# inventory.update_variant_inventory_tracking). `resolved_any=False` covers
# the case where nothing the caller requested ever resolved to a target, so
# `failed` is replaced with the `unresolved` count instead.


def test_outcome_header_all_succeeded_is_byte_identical_confirmed_header() -> None:
    """failed=0 must render exactly like the pre-9.24 unconditional header —
    no counts appended — so every all-succeeded (and no-op/idempotent, 0/0)
    call site's output is unchanged."""
    assert _wt._outcome_header("Publish product to channels", 3, 0) == (
        "CONFIRMED — Publish product to channels"
    )


def test_outcome_header_zero_attempted_counts_as_confirmed() -> None:
    """An idempotent no-op (nothing needed changing, so nothing was attempted)
    is 0 succeeded / 0 failed — trivially not a rejection."""
    assert _wt._outcome_header("Publish product to channels", 0, 0) == (
        "CONFIRMED — Publish product to channels"
    )


def test_outcome_header_none_succeeded_returns_failed_marker() -> None:
    """succeeded=0 with failed>0 — every attempted item was rejected — must
    never read CONFIRMED."""
    out = _wt._outcome_header("Publish product to channels", 0, 2)
    assert out == "FAILED — Publish product to channels"
    assert "CONFIRMED" not in out


def test_outcome_header_partial_reports_both_counts() -> None:
    out = _wt._outcome_header("Set product publications (declarative)", 1, 1)
    assert out == "PARTIAL — Set product publications (declarative) (1 succeeded, 1 failed)"
    assert "CONFIRMED" not in out
    assert "FAILED" not in out


def test_outcome_header_resolved_any_false_replaces_failed_with_unresolved() -> None:
    """resolved_any=False: `failed` (999, deliberately unreachable) is
    replaced with `unresolved`, not merged with `succeeded` — pins the exact
    numbers so a helper that swaps which count gets replaced is caught."""
    out = _wt._outcome_header(
        "Publish product to channels", 3, 999, unresolved=5, resolved_any=False
    )
    assert out == "PARTIAL — Publish product to channels (3 succeeded, 5 failed)"


def test_outcome_header_resolved_any_false_zero_unresolved_is_confirmed() -> None:
    """resolved_any=False with unresolved=0 is the pure no-op case (nothing
    requested at all) — still CONFIRMED via the 0/0 rule."""
    out = _wt._outcome_header(
        "Publish product to channels", 3, 999, unresolved=0, resolved_any=False
    )
    assert out == "CONFIRMED — Publish product to channels"


def test_outcome_header_resolved_any_true_ignores_unresolved() -> None:
    """resolved_any=True (the default) uses `failed` as given, regardless of
    `unresolved` — at least one requested item resolved, so pre-mutation
    resolve failures are the caller's to render elsewhere, not this
    helper's to count."""
    out = _wt._outcome_header("Publish product to channels", 1, 0, unresolved=5, resolved_any=True)
    assert out == "CONFIRMED — Publish product to channels"
