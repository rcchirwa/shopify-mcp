"""Session-wide pytest fixtures for shopify-mcp offline tests."""

import logging
from collections.abc import Generator
from pathlib import Path

import pytest

import shopify_mcp.tools._write_tool as _wt
from shopify_mcp.tools import _log

# Keep the live smoke runners out of default discovery (Story 10.45 / FS-1).
# They need SHOPIFY_STORE_URL + SHOPIFY_ACCESS_TOKEN against a real store, so
# they'd fail in CI and on any machine without a configured .env.
#
# This lives here rather than as `--ignore=tests/live` in pyproject.toml because
# pytest resolves a relative --ignore against the *invocation* directory, not
# the rootdir: `cd /tmp && pytest ~/shopify-mcp` would silently stop ignoring
# them and fail five tests on missing credentials. `collect_ignore` is resolved
# relative to the conftest that declares it, so it holds from any working
# directory. Naming the directory (not the files in it) is the point — adding a
# live runner is a `git add` under tests/live/, not a config edit.
#
# Explicitly asking for them still works: `pytest tests/live`.
collect_ignore = ["live"]


# Captured at conftest import — before any fixture below can run — so it holds
# the value _log.py computes for production. Once _redirect_audit_log is active
# the live attribute no longer answers "where would the server write?", and
# tests/architecture/test_src_layout.py needs that answer to keep pinning the
# parents[3] depth (Story 10.47 / FS-3).
_PRODUCTION_LOG_FILE = _log.LOG_FILE


@pytest.fixture(scope="session")
def production_log_file() -> str:
    """`_log.LOG_FILE` as imported, before the per-test redirect replaced it."""
    return _PRODUCTION_LOG_FILE


def _reset_audit_logger() -> None:
    """Drop _log's cached logger and close its handler.

    _get_logger() memoises (logger, path) and only rebuilds when LOG_FILE
    changes by value. Two things go wrong without an explicit reset: a handler
    left open on a tmp_path file leaks a descriptor for the rest of the run
    (once per logging test), and a handler we closed but left installed would
    make the next log_write write to a closed stream. Clearing the cache forces
    a clean rebuild against whatever LOG_FILE holds at that moment.

    Assigns the module globals directly rather than through monkeypatch: the
    point is to leave the cache empty, and monkeypatch would faithfully restore
    the stale pre-test logger — the very object whose handler was just closed.
    """
    if _log._logger is not None:
        for handler in _log._logger.handlers:
            handler.close()
        _log._logger.handlers.clear()
    _log._logger = None
    _log._current_log_file = None


@pytest.fixture(autouse=True)
def _redirect_audit_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Generator[None, None, None]:
    """Send audit lines to a per-test tmp file, never the real trail.

    `_log.LOG_FILE` resolves to the repo root of whatever checkout runs the
    tests. In the main checkout that is the same `aon_mcp_log.txt` the live
    shopify-gss server appends real Shopify writes to, so before Story 10.93 a
    full offline run added 63 fixture lines to a durable audit record that
    OPEN_STORIES.md cites as evidence, on a log whose rotation is capped at
    10 MB x 5 — test churn evicted genuine history.

    Patching `log_write` per module cannot close this: eleven tool modules do
    `from shopify_mcp.tools._log import log_write`, so each binding has to be
    patched separately and a module added later silently reopens the hole. This
    redirects the one place the path is decided, which every call site reaches
    through — including call sites that do not exist yet. The real logging code
    still runs, so _log.py's own behaviour stays exercised rather than stubbed.

    Per-module `_no_log_write` fixtures and `_no_write_gate_log` below are left
    in place: several tests assert on the calls they intercept, and this fixture
    is a backstop for the paths nothing else covers, not a replacement.

    Guarded by tests/unit/tools/test_log_isolation.py — remove this and those
    two tests fail.
    """
    _reset_audit_logger()
    monkeypatch.setattr(_log, "LOG_FILE", str(tmp_path / "aon_mcp_log.txt"))
    yield
    # Before monkeypatch restores the real LOG_FILE, so no handler is left
    # pointing at the tmp file pytest is about to reap.
    _reset_audit_logger()


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
    from shopify_mcp import logging_config as _lc

    _lc._configured = False
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if type(handler) is logging.StreamHandler:
            handler.close()
            root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    for name in ("gql", "urllib3", "requests"):
        logging.getLogger(name).setLevel(logging.NOTSET)
