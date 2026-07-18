"""
Offline unit tests for tools/_filters.py — the HTML-safety detector.

Story 10.35 / SEC-M2-sanitizer (Approach 1, detection-only): the substring
blocklist is wrapped by a parser-based detector that diffs the input against
``nh3.clean`` and flags script-executing or look-alike URL schemes, so creative
payloads that dodge the curated blocklist are surfaced in the preview warning.
No content is stripped — these findings are advisory only.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_filters_offline.py -v
"""

from tools import _filters
from tools._filters import (
    dangerous_html_patterns,
    html_safety_findings,
    html_strip_report,
    sanitize_html,
)

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
    check still flags it as a non-ASCII look-alike."""
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


def test_url_scheme_ignores_browser_stripped_whitespace():
    """Browsers drop tab / CR / LF from a URL before resolving its scheme, so the
    helper must too — otherwise `java<TAB>script:` reads as a relative URL."""
    assert _filters._url_scheme("java\tscript:alert(1)") == "javascript"
    assert _filters._url_scheme("java\nscript:alert(1)") == "javascript"


# ---------- benign real-world markup must not raise noise (alert fatigue) ----------


def test_benign_attributes_do_not_warn():
    """nh3 strips class / id / data-* / align / width for conformance, but they
    are not injection vectors — flagging them would bury the real findings."""
    payload = (
        '<div class="rte" id="main" data-section="x" align="center"><p width="600">hi</p></div>'
    )
    assert html_safety_findings(payload) == []


def test_realistic_rich_text_description_stays_silent():
    """A typical theme / rich-text product description (classes, data-*, an
    https image) produces no warning."""
    payload = (
        '<div class="product-description">'
        '<p class="rte__p">Soft combed cotton. <strong>Pre-shrunk.</strong></p>'
        '<img src="https://cdn.shopify.com/x.png" class="lazyload" width="800" alt="tee">'
        "</div>"
    )
    assert html_safety_findings(payload) == []


def test_data_image_uri_does_not_warn():
    """Inline data:image/* (common in descriptions) is benign and must stay
    silent — only data:text/html is dangerous, and the blocklist owns that."""
    payload = '<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==" alt="x">'
    assert html_safety_findings(payload) == []


# ---------- scheme obfuscation a browser still executes ----------


def test_tab_obfuscated_javascript_scheme_is_flagged():
    """A tab inside 'javascript:' breaks the substring blocklist, but a browser
    strips the tab and runs it — so it must still warn."""
    payload = '<a href="java\tscript:alert(1)">x</a>'
    assert dangerous_html_patterns(payload) == []  # the tab breaks the 'javascript:' substring
    findings = html_safety_findings(payload)
    assert any("scheme" in f and "href=" in f for f in findings)


# ---------- sanitize_html / html_strip_report (Approach 2, post-sign-off) ----------
#
# Product sign-off recorded on the Trello card (4vEAwQWo, 2026-07-17, amended
# same day): p, br, b, i, em, strong, u, ul, ol, li, h1-h6, a[href,title],
# span[style,class], div[class], img[src,alt], table/tr/td/th. href/src
# restricted to http/https. style limited to color/font-weight. Strip-and-warn:
# disallowed markup is removed by sanitize_html() before the Shopify write.


def test_sanitize_html_preserves_allowed_rich_text():
    payload = "<p>Hello <b>world</b> <i>there</i></p>"
    assert sanitize_html(payload) == payload


def test_sanitize_html_preserves_realistic_theme_description():
    """The same fixture html_safety_findings already treats as benign real-world
    content must survive sanitize_html unchanged in substance (class/img kept)."""
    payload = (
        '<div class="product-description">'
        "<p>Soft combed cotton. <strong>Pre-shrunk.</strong></p>"
        '<img src="https://cdn.shopify.com/x.png" alt="tee">'
        "</div>"
    )
    assert sanitize_html(payload) == payload


def test_sanitize_html_strips_style_tag():
    assert "<style" not in sanitize_html("<p>hi</p><style>body{color:red}</style>")


def test_sanitize_html_strips_iframe():
    assert "<iframe" not in sanitize_html('<iframe src="https://evil.example"></iframe>')


def test_sanitize_html_strips_event_handler_attribute():
    sanitized = sanitize_html('<img src="https://cdn.shopify.com/x.png" onerror="alert(1)">')
    assert "onerror" not in sanitized
    assert "https://cdn.shopify.com/x.png" in sanitized


def test_sanitize_html_strips_event_handler_even_on_allowed_tag():
    """on*= must never survive even though <div> is itself allowed."""
    assert "onmouseover" not in sanitize_html('<div onmouseover="alert(1)">hi</div>')


def test_sanitize_html_strips_javascript_scheme_href():
    sanitized = sanitize_html('<a href="javascript:alert(1)">click</a>')
    assert "javascript:" not in sanitized


def test_sanitize_html_strips_javascript_scheme_img_src():
    sanitized = sanitize_html('<img src="javascript:alert(1)">')
    assert "javascript:" not in sanitized


def test_sanitize_html_strips_unicode_lookalike_scheme():
    sanitized = sanitize_html(f'<a href="{_CYRILLIC_JE}avascript:alert(1)">x</a>')
    assert "href" not in sanitized


def test_sanitize_html_allows_relative_href():
    """No scheme to reject — relative URLs pass through."""
    sanitized = sanitize_html('<a href="/collections/all">shop</a>')
    assert 'href="/collections/all"' in sanitized


def test_sanitize_html_style_attribute_keeps_allowed_properties_only():
    sanitized = sanitize_html('<span style="color:red;position:absolute;font-weight:bold">x</span>')
    assert "color:red" in sanitized
    assert "font-weight:bold" in sanitized
    assert "position" not in sanitized


def test_sanitize_html_strips_disallowed_tag_not_in_allowlist():
    """<form> is a genuine structural-injection vector and is not on the
    allow-list even though it isn't the curated blocklist's concern."""
    assert "<form" not in sanitize_html('<form action="https://evil.example">hi</form>')


def test_sanitize_html_plain_text_passthrough():
    assert sanitize_html("Vanish Trucker Hat | Streetwear") == "Vanish Trucker Hat | Streetwear"


def test_html_strip_report_empty_for_fully_allowed_content():
    assert html_strip_report("<p>Hello <b>world</b></p>") == []


def test_html_strip_report_empty_for_plain_text():
    assert html_strip_report("Only Title") == []


def test_html_strip_report_names_stripped_tag():
    report = html_strip_report("<p>hi</p><style>body{color:red}</style>")
    assert any("style" in item for item in report)


def test_html_strip_report_names_stripped_attribute():
    report = html_strip_report('<img src="https://cdn.shopify.com/x.png" onerror="alert(1)">')
    assert any("onerror" in item and "img" in item for item in report)


def test_html_strip_report_does_not_flag_safety_additions():
    """nh3 adds rel="noopener noreferrer" to <a> — that's an addition, not a
    removal, and must not appear as a stripped-content finding."""
    report = html_strip_report('<a href="https://example.com">shop</a>')
    assert report == []
