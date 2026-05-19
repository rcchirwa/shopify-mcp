"""
Offline unit tests for tools/_log.py.

Pins the sanitization contract: control characters in the `description`
argument must not break the line-oriented audit log format. Without this,
a caller passing a product_id like "100\\n[INJECT]" could forge a second
log line.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_log_offline.py -v
"""

import logging.handlers
from pathlib import Path

import pytest

from tools import _log


@pytest.fixture
def tmp_log(monkeypatch, tmp_path):
    """Redirect log_write to a tmp file; force handler re-init."""
    target = tmp_path / "test.log"
    # Close any open handler before redirecting so the new path takes effect.
    if _log._logger is not None:
        for h in _log._logger.handlers:
            h.close()
    monkeypatch.setattr(_log, "_logger", None)
    monkeypatch.setattr(_log, "_current_log_file", None)
    monkeypatch.setattr(_log, "LOG_FILE", str(target))
    yield target
    # Close handler opened for the tmp file before monkeypatch restores state.
    if _log._logger is not None:
        for h in _log._logger.handlers:
            h.close()
    monkeypatch.setattr(_log, "_logger", None)
    monkeypatch.setattr(_log, "_current_log_file", None)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_log_write_appends_a_single_line(tmp_log):
    _log.log_write("update_product_pricing", "product=100 variants=2")
    contents = _read(tmp_log)
    assert contents.endswith("\n")
    assert contents.count("\n") == 1
    assert "update_product_pricing | product=100 variants=2" in contents


def test_log_write_escapes_newlines_in_description(tmp_log):
    # Newlines in caller-supplied identifiers must not forge log lines.
    _log.log_write("update_product_pricing", "product=100\nFAKE LOG LINE")
    contents = _read(tmp_log)
    assert contents.count("\n") == 1  # Only the terminating newline.
    assert "\\n" in contents  # Escaped form is present.
    assert "FAKE LOG LINE" in contents  # Payload preserved, just escaped.


def test_log_write_escapes_carriage_returns(tmp_log):
    _log.log_write("t", "x=1\rINJECT")
    contents = _read(tmp_log)
    assert "\\r" in contents
    assert contents.count("\n") == 1


def test_log_write_appends_across_multiple_calls(tmp_log):
    _log.log_write("t", "first")
    _log.log_write("t", "second")
    contents = _read(tmp_log)
    assert contents.count("\n") == 2
    assert "first" in contents
    assert "second" in contents


def test_rotating_handler_is_configured(tmp_log):
    _log.log_write("t", "init")  # trigger lazy-init
    assert len(_log._logger.handlers) == 1
    handler = _log._logger.handlers[0]
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == _log._MAX_BYTES
    assert handler.backupCount == _log._BACKUP_COUNT


def test_logger_reinitializes_when_log_file_path_changes(tmp_path):
    # Uses manual save/restore instead of monkeypatch because we need to observe
    # module state *between* two log_write calls — monkeypatch only restores at
    # teardown, after the assertions have already run.
    path_a = tmp_path / "a.log"
    path_b = tmp_path / "b.log"
    saved_log_file = _log.LOG_FILE
    saved_logger = _log._logger
    saved_current_log_file = _log._current_log_file
    try:
        if _log._logger is not None:
            for h in _log._logger.handlers:
                h.close()
        _log._logger = None
        _log._current_log_file = None

        _log.LOG_FILE = str(path_a)
        _log.log_write("t", "to-a")

        # Switch path without resetting _logger — reinit must fire
        _log.LOG_FILE = str(path_b)
        _log.log_write("t", "to-b")

        assert "to-a" in path_a.read_text(encoding="utf-8")
        assert "to-b" in path_b.read_text(encoding="utf-8")
    finally:
        if _log._logger is not None:
            for h in _log._logger.handlers:
                h.close()
        _log._logger = saved_logger
        _log._current_log_file = saved_current_log_file
        _log.LOG_FILE = saved_log_file
