"""Session-wide pytest fixtures for shopify-mcp offline tests."""

import itertools
import logging
import shutil
import tempfile
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

# Resolved from this conftest, like collect_ignore above, so it holds from any
# working directory. `_redirect_audit_log` exempts tests under here: they mutate
# a real store and their audit lines are the genuine record of it.
_LIVE_TESTS_DIR = Path(__file__).resolve().parent / "live"


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


_audit_seq = itertools.count()
_audit_dir: str | None = None


def _audit_redirect_dir() -> Path:
    """One session directory holding every redirected audit file.

    A directory per test (`tmp_path`) cost 81x the basetemp entries and ~40% of
    the suite's wall clock for 2116 tests, almost all of which never log at all
    — the handler is only built on the first `log_write`. One directory with a
    file per test keeps writers isolated from each other at a fraction of that.
    """
    global _audit_dir
    if _audit_dir is None:
        _audit_dir = tempfile.mkdtemp(prefix="shopify-mcp-audit-")
    return Path(_audit_dir)


def _invocation_targets_live_tests(config: pytest.Config) -> bool:
    """Whether this pytest invocation names anything under tests/live/.

    Those runners mutate a real store and must log for real, so a run that asks
    for them does not get the session-wide redirect below. They are out of
    default discovery, so this is False for a plain `pytest` and for CI.
    """
    for arg in config.args:
        raw = Path(str(arg).split("::", 1)[0])
        if not raw.is_absolute():
            raw = Path(config.invocation_params.dir) / raw
        try:
            resolved = raw.resolve()
        except OSError:
            # A path arg that cannot be resolved is not a live-test path. No
            # pragma: the coverage gate measures `shopify_mcp` only, so nothing
            # under tests/ is reported either way.
            continue
        if resolved == _LIVE_TESTS_DIR or _LIVE_TESTS_DIR in resolved.parents:
            return True
    return False


def pytest_configure(config: pytest.Config) -> None:
    """Redirect the audit log for the whole session, before collection starts.

    `_redirect_audit_log` below is function-scoped, and three routes get past a
    function-scoped fixture with the suite still green — each reproduced by an
    independent verifier on 2026-09-18:

    * a higher-scoped (session/package/module/class) autouse fixture that drives
      a confirmed write during setup,
    * a test module that defines its own `_redirect_audit_log` and so shadows
      this one by name,
    * anything that logs at import or collection time.

    Setting LOG_FILE here means the production path is not the target from the
    moment pytest configures, whatever happens to the per-test fixture. That
    fixture still runs, narrowing the redirect to one file per test so writers
    cannot read each other's lines.

    Skipped when the invocation names tests/live — see
    `_invocation_targets_live_tests`. Deliberately not a post-hoc "did the real
    log change?" check: the live server appends to that file, so a comparison
    would fail on someone else's genuine write.
    """
    if _invocation_targets_live_tests(config):
        return
    _log.LOG_FILE = str(_audit_redirect_dir() / "session.log")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Undo pytest_configure's redirect and drop the session directory."""
    global _audit_dir
    _log.LOG_FILE = _PRODUCTION_LOG_FILE
    _reset_audit_logger()
    if _audit_dir is not None:
        shutil.rmtree(_audit_dir, ignore_errors=True)
        _audit_dir = None


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
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Send offline audit lines to a per-test tmp file, never the real trail.

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

    **tests/live/ is deliberately exempt.** Those runners mutate a real store —
    they register and delete a live webhook, create a collection, publish and
    unpublish one — and each tool's audit line is the durable record that the
    store was touched. Redirecting them would strip genuine entries from the
    very trail this fixture exists to keep trustworthy, which is the opposite
    of the point. They are out of default discovery (`collect_ignore` above),
    so they only run when asked for by name, and then they should log for real.

    **This fixture gives per-test isolation, not the outer safety net.** Being
    function-scoped, it cannot cover a higher-scoped fixture that writes during
    setup, collection-time logging, or a test module that shadows its name.
    `pytest_configure` above redirects the whole session for exactly those three
    routes; this narrows that to one file per test so writers cannot read each
    other's lines.

    Guarded by tests/unit/tools/test_log_isolation.py — remove this and those
    two tests fail.
    """
    # .resolve() both sides: _LIVE_TESTS_DIR is resolved, and an invocation
    # through a symlinked path (macOS /tmp -> /private/tmp) yields an
    # unresolved request.path, which would silently miss the exemption and
    # redirect a real live-store write.
    if _LIVE_TESTS_DIR in request.path.resolve().parents:
        yield
        return
    _reset_audit_logger()
    monkeypatch.setattr(_log, "LOG_FILE", str(_audit_redirect_dir() / f"t{next(_audit_seq)}.log"))
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
