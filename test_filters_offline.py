"""
Offline unit tests for tools/_filters.py — the HTML-safety detector.

Story 10.35 / SEC-M2-sanitizer (Approach 1, detection-only): the substring
blocklist is wrapped by a parser-based detector that diffs the input against
``nh3.clean`` and vets URL schemes against nh3's allow-list, so creative
payloads that dodge the curated blocklist are surfaced in the preview warning.
No content is stripped — these findings are advisory only.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_filters_offline.py -v
"""

from tools import _filters
from tools._filters import dangerous_html_patterns, html_safety_findings

# U+0458 CYRILLIC SMALL LETTER JE — visually identical to ASCII 'j'. Built with
# chr() so the source stays ASCII (no ruff RUF001 ambiguous-glyph) while the
# runtime string is the genuine look-alike payload.
_CYRILLIC_JE = chr(0x0458)


# ---------- known bypass classes now produce findings (AC2) ----------


def test_css_style_tag_is_flagged():
    """<style>/CSS injection is invisible to the substring blocklist but nh3
    strips the tag, so the diff surfaces it."""
    payload = "<p>hi</p><style>body{color:red}</style>"
    assert dangerous_html_patterns(payload) == []  # blocklist is blind to it
    findings = html_safety_findings(payload)
    assert any("style" in f for f in findings)


def test_style_attribute_injection_is_flagged():
    """A style= attribute (CSS-injection vector) the blocklist never knew."""
    payload = '<p style="color:red">hi</p>'
    assert dangerous_html_patterns(payload) == []
    findings = html_safety_findings(payload)
    assert any("style=" in f and "<p>" in f for f in findings)


def test_unicode_lookalike_scheme_is_flagged():
    """A Cyrillic je (U+0458) in the javascript scheme dodges the ASCII
    blocklist, and nh3 itself leaves the href in place — but the URL-scheme
    check (vs nh3's allow-list) still flags it."""
    payload = f'<a href="{_CYRILLIC_JE}avascript:alert(1)">x</a>'
    assert dangerous_html_patterns(payload) == []  # no ASCII "javascript:" substring
    findings = html_safety_findings(payload)
    assert any("scheme" in f and "href=" in f for f in findings)


def test_disallowed_tag_unknown_to_blocklist_is_flagged():
    """<base> isn't in the curated blocklist, but nh3 strips it (AC6: nh3 is the
    engine, not the substring list)."""
    payload = '<base href="https://evil.example/">'
    assert dangerous_html_patterns(payload) == []
    findings = html_safety_findings(payload)
    assert any("base" in f for f in findings)


# ---------- safe content stays silent (AC4) ----------


def test_safe_formatting_html_has_no_findings():
    assert html_safety_findings("<p>Hello <b>world</b> <i>there</i></p>") == []


def test_plain_text_with_accents_and_emoji_has_no_findings():
    """Normal non-ASCII product copy must not trip the look-alike scheme check."""
    assert html_safety_findings("Café — naïve ™ 🎉 handcrafted in Zürich") == []


def test_allowed_https_link_has_no_findings():
    assert html_safety_findings('<a href="https://example.com">shop</a>') == []


def test_relative_link_has_no_findings():
    """A scheme-less (relative) href must not be flagged."""
    assert html_safety_findings('<a href="/collections/all">shop</a>') == []


def test_safe_link_with_generic_attrs_has_no_findings():
    """nh3 keeps generic attrs (title/lang) even though ALLOWED_ATTRIBUTES has
    no <p> entry — the detector must not over-flag them."""
    assert html_safety_findings('<p title="t" lang="en">hi</p>') == []


# ---------- no regression: blocklist findings still fire (AC5) ----------


def test_blocklist_script_token_still_present():
    findings = html_safety_findings("<script>alert(1)</script>")
    assert "<script" in findings


def test_event_handler_flagged_exactly_once():
    """on*= is a blocklist token; the sanitizer diff must not duplicate it."""
    findings = html_safety_findings('<div onmouseover="x">hi</div>')
    assert findings.count("onmouseover=") == 1


def test_ascii_javascript_scheme_reported_once_via_blocklist():
    """ASCII javascript: is owned by the blocklist; the scheme check must not
    add a duplicate finding for it."""
    findings = html_safety_findings('<a href="javascript:alert(1)">x</a>')
    assert "javascript:" in findings
    assert not any("scheme" in f for f in findings)


# ---------- self-closing tags exercise handle_startendtag ----------


def test_self_closing_disallowed_tag_is_flagged():
    findings = html_safety_findings('<embed src="https://evil.example"/>')
    assert any("embed" in f for f in findings)


# ---------- _url_scheme helper edge cases ----------


def test_url_scheme_no_colon_returns_none():
    assert _filters._url_scheme("/relative/path") is None


def test_url_scheme_slash_before_colon_returns_none():
    assert _filters._url_scheme("path/to:thing") is None


def test_url_scheme_extracts_ascii_scheme():
    assert _filters._url_scheme("https://example.com") == "https"
