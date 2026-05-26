"""Session-wide pytest fixtures for shopify-mcp offline tests."""

import logging
from collections.abc import Generator

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


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Generator[None, None, None]:
    """Reset logging state added by configure_logging() after each test.

    configure_logging() uses a module-level _configured flag for idempotency.
    Without cleanup, the StreamHandler from the first ShopifyClient() test
    persists, pointing to the wrong sys.stderr for capsys in subsequent tests.

    Only removes plain logging.StreamHandler instances (ours) — pytest's own
    LogCaptureHandler subclasses are left untouched.

    Setup is intentionally empty: _configured starts False at module import.
    Teardown resets to that initial state so each test gets a clean slate.
    """
    yield
    import logging_config as _lc

    _lc._configured = False
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if type(handler) is logging.StreamHandler:
            handler.close()
            root.removeHandler(handler)
    root.setLevel(logging.WARNING)
