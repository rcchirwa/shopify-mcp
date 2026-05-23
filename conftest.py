"""Session-wide pytest fixtures for shopify-mcp offline tests."""

import pytest

import tools._write_tool as _wt


@pytest.fixture(autouse=True)
def _no_write_gate_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent write_gate() from writing to aon_mcp_log.txt during tests.

    Tools migrated to write_gate() call log_write through _write_tool, not
    their own module. This fixture patches the single call-site so all
    migrated tools are covered without per-test setup.

    Per-module _no_log_write fixtures in individual test files cover tools
    that still call log_write directly (non-migrated tools).
    """
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: None)
