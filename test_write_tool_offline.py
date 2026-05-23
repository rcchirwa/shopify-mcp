"""
Offline unit tests for tools/_write_tool.py — write_gate() helper.

write_gate() is a pure control-flow function; tests exercise it directly
without going through a registered MCP tool or FakeClient.
"""

import pytest

import tools._write_tool as _wt
from shopify_client import TransientShopifyError
from tools._response import with_confirm_hint


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


def test_confirm_returns_done_preview_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert out == "Done. PREVIEW — update title"
    assert logged == [("update_product_title", "id=1 | 'A' → 'B'")]


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
    assert out_done == "Done. PREVIEW — x"
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
