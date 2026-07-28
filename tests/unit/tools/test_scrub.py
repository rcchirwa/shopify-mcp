"""
Offline unit tests for tools/_scrub.py.

Pins the shared reflected-text cap: exception/response/user text echoed back
to the caller or written to the audit log must be bounded, so an
attacker-controlled multi-KB field can neither flood the rotating log nor
leak large upstream bodies (signed-URL fragments, internal host detail) into
model context. Normal-length text must pass through byte-for-byte.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_scrub.py -v
"""

from shopify_mcp.tools import _scrub


def test_cap_leaves_short_text_unchanged():
    assert _scrub.cap("plain error message") == "plain error message"


def test_cap_truncates_to_default_reflect_limit():
    oversized = "Z" * (_scrub.REFLECT_MAX_LEN + 500)
    capped = _scrub.cap(oversized)
    assert len(capped) == _scrub.REFLECT_MAX_LEN
    assert capped == "Z" * _scrub.REFLECT_MAX_LEN


def test_cap_honours_explicit_limit():
    assert _scrub.cap("abcdefghij", 4) == "abcd"


def test_cap_at_exactly_the_limit_is_unchanged():
    exact = "A" * _scrub.REFLECT_MAX_LEN
    assert _scrub.cap(exact) == exact


def test_sanitize_control_chars_escapes_newline():
    assert _scrub.sanitize_control_chars("a\nb") == "a\\nb"


def test_sanitize_control_chars_escapes_carriage_return():
    assert _scrub.sanitize_control_chars("a\rb") == "a\\rb"


def test_sanitize_control_chars_escapes_both_in_combination():
    assert _scrub.sanitize_control_chars("a\r\nb\n\rc") == "a\\r\\nb\\n\\rc"


def test_sanitize_control_chars_no_op_on_clean_text():
    assert _scrub.sanitize_control_chars("plain text, no control chars") == (
        "plain text, no control chars"
    )
