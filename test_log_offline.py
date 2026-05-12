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

from pathlib import Path

import pytest

from tools import _log


@pytest.fixture
def tmp_log(monkeypatch, tmp_path):
    """Redirect log_write to a tmp file so the real audit log isn't touched."""
    target = tmp_path / "test.log"
    monkeypatch.setattr(_log, "LOG_FILE", str(target))
    return target


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
