"""
Shared filtering helpers used by multiple tool modules.
"""

import html.parser
import re
from typing import Any

import nh3

from tools._gid import to_gid

# Exact substrings checked case-insensitively.  Event-handler attributes
# (onclick=, onload=, onmouseover=, etc.) are handled separately by the regex
# below so that space-before-= variants ("onload =") and any future on* names
# are caught without maintaining an exhaustive list.
_DANGEROUS_HTML_EXACT = (
    "<script",
    "javascript:",
    "vbscript:",
    "data:text/html",
    "<iframe",
    "<object",
    "<embed",
    "</title>",
)

# Matches any HTML event-handler attribute, including space before "=" and
# mixed-case names.  Examples: onclick=, onLoad =, ONMOUSEOVER=, ontoggle=.
_RE_ON_HANDLER = re.compile(r"\bon\w+\s*=", re.IGNORECASE)


def dangerous_html_patterns(text: str) -> list[str]:
    """Return dangerous HTML substrings / patterns found in *text* (case-insensitive).

    Returns a deduplicated list of matched strings suitable for display in
    operator-facing warning messages.  The list combines:
    - exact substring matches (lower-cased pattern names), and
    - on*= event-handler attribute matches (lower-cased actual matches).
    """
    lower = text.lower()
    found: list[str] = [p for p in _DANGEROUS_HTML_EXACT if p in lower]

    seen: set[str] = set(found)
    for match in _RE_ON_HANDLER.findall(text):
        key = match.lower().rstrip()  # normalise trailing space before "="
        if key not in seen:
            found.append(key)
            seen.add(key)

    return found


# URL-bearing attributes whose value carries a scheme worth vetting. Kept
# deliberately small — these are the attributes a storefront theme renders as a
# navigable / loadable URL.
_URL_ATTRS = frozenset(
    {"href", "src", "xlink:href", "action", "formaction", "poster", "background", "cite"}
)

# Attributes that are a genuine injection vector when a sanitizer strips them.
# `style` is the CSS-injection vector this story calls out; event-handler (on*=)
# attributes are already covered by the substring blocklist. Benign formatting
# attributes nh3 also strips (class / id / data-* / align / width / role / aria)
# are deliberately NOT flagged — warning on every non-conforming attribute is
# alert-fatigue noise that would bury the real findings.
_DANGEROUS_STRIPPED_ATTRS = frozenset({"style"})

# Schemes that execute script when a browser navigates to them. `data:text/html`
# stays with the substring blocklist; benign `data:image/*` URIs must not warn.
_ACTIVE_URL_SCHEMES = frozenset({"javascript", "vbscript"})

# Characters a browser strips from a URL before resolving its scheme, so
# `java<TAB>script:` runs as `javascript:`. Removing them first means the scheme
# check sees what the browser will, not the obfuscated source.
_SCHEME_IGNORED_CHARS = {ord(c): None for c in "\t\n\r"}


def _url_scheme(value: str) -> str | None:
    """Return the scheme of *value* (the text before the first ``:``) as a
    browser would resolve it, or ``None`` when there is no scheme.

    Browser-ignored whitespace (tab / CR / LF) is removed and the value is
    stripped first, so ``java<TAB>script:`` resolves to ``javascript``. A
    path / query / fragment / space before the colon means it is a relative URL
    (``/a:b``, ``#a:b``), not a scheme.
    """
    cleaned = value.translate(_SCHEME_IGNORED_CHARS).strip()
    colon = cleaned.find(":")
    if colon <= 0:
        return None
    head = cleaned[:colon]
    if any(c in head for c in "/?# "):
        return None
    return head


def _scheme_is_suspect(scheme: str) -> bool:
    """A scheme is suspect when it executes script (``javascript:`` /
    ``vbscript:``) or carries a non-ASCII look-alike character — legitimate URL
    schemes are ASCII, so a non-ASCII scheme is an evasion attempt."""
    return scheme.casefold() in _ACTIVE_URL_SCHEMES or not scheme.isascii()


class _SanitizerIndexer(html.parser.HTMLParser):
    """Index one HTML string into a tag → attribute-name map plus its URL-bearing
    attribute values, so the input and ``nh3.clean(input)`` can be diffed for
    what the sanitizer would strip. Tag and attribute names arrive lower-cased
    from ``HTMLParser``, matching nh3's lower-case allow-lists.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tag_attrs: dict[str, set[str]] = {}
        self.url_values: list[tuple[str, str, str]] = []

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = self.tag_attrs.setdefault(tag, set())
        for name, value in attrs:
            names.add(name)
            if name in _URL_ATTRS and value:
                self.url_values.append((tag, name, value))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)


def _index_html(text: str) -> _SanitizerIndexer:
    indexer = _SanitizerIndexer()
    indexer.feed(text)
    indexer.close()
    return indexer


def html_safety_findings(text: str) -> list[str]:
    """Advisory HTML-safety findings for *text* — detection only, nothing is
    stripped (Story 10.35 / SEC-M2-sanitizer, Approach 1).

    Wraps the substring :func:`dangerous_html_patterns` blocklist (kept so its
    existing warnings still fire) with a parser-based detector that uses ``nh3``
    as the engine:

    1. **Structural diff** — parse the input and ``nh3.clean(input)`` and report
       (a) every *tag* the sanitizer would strip (``<style>`` / CSS injection,
       ``<iframe>``, ``<base>``, ``<form>``, … — far beyond the curated list) and
       (b) the dangerous *attributes* it would strip (``style``; on*= handlers
       already come from the blocklist). Benign formatting attributes nh3 also
       strips (``class`` / ``id`` / ``data-*`` / …) are intentionally not
       flagged, to keep the warning signal high.
    2. **URL-scheme check** — flag URL-bearing attributes whose scheme executes
       script (``javascript:`` / ``vbscript:``, including tab / newline-obfuscated
       forms a browser still runs) or uses a non-ASCII look-alike (e.g. a
       Cyrillic je, U+0458, standing in for the 'j' of ``javascript:``).

    Findings already named by the blocklist (a ``<tag`` prefix, an ``attr=``
    handler token, or an ASCII ``scheme:``) are not repeated.
    """
    findings: list[str] = list(dangerous_html_patterns(text))
    seen: set[str] = set(findings)

    def _add(message: str) -> None:
        if message not in seen:
            seen.add(message)
            findings.append(message)

    before = _index_html(text)
    after = _index_html(nh3.clean(text))

    # Tags and dangerous attributes nh3 would strip. Tag findings catch <style>,
    # <iframe>, <base>, <form>, … (anything outside nh3's allow-list); attribute
    # findings are limited to genuine injection vectors (_DANGEROUS_STRIPPED_ATTRS)
    # so benign formatting attributes don't raise noise.
    for tag, names in before.tag_attrs.items():
        if tag not in after.tag_attrs:
            # The blocklist may already name this tag (e.g. '<script'); only add
            # the ones it does not know (e.g. '<style>').
            if f"<{tag}" not in seen:
                _add(f"<{tag}> (tag stripped by sanitizer)")
            continue
        for name in sorted(names & _DANGEROUS_STRIPPED_ATTRS):
            if name not in after.tag_attrs[tag] and f"{name}=" not in seen:
                _add(f"{name}= on <{tag}> (attribute stripped by sanitizer)")

    # URL schemes that execute (javascript:/vbscript:, including tab / newline-
    # obfuscated forms) or use a non-ASCII look-alike. ASCII active schemes the
    # blocklist already names are not repeated.
    for tag, name, value in before.url_values:
        scheme = _url_scheme(value)
        if scheme is None or not _scheme_is_suspect(scheme):
            continue
        if f"{scheme.casefold()}:" in seen:
            continue
        _add(f"{name}= on <{tag}> uses suspicious URL scheme {scheme!r}")

    return findings


# Story 10.35 / SEC-M2-sanitizer, Approach 2 (post-sign-off) — allow-list
# recorded on the Trello card (4vEAwQWo, 2026-07-17, amended same day). Applies
# to descriptionHtml (products, collections) and seo.title / seo.description.
# Standard rich-text formatting plus images/tables/wrapper divs, since real
# theme-authored descriptions rely on them (see test_realistic_rich_text_
# description_stays_silent) — event-handler attributes and script-executing /
# non-http(s) URL schemes are never in any tag's attribute set, so they are
# stripped regardless of which tags are allowed.
_ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "b",
        "i",
        "em",
        "strong",
        "u",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "a",
        "span",
        "div",
        "img",
        "table",
        "tr",
        "td",
        "th",
    }
)
_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title"},
    "span": {"style", "class"},
    "div": {"class"},
    "img": {"src", "alt"},
}
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_ALLOWED_STYLE_PROPERTIES = frozenset({"color", "font-weight"})


def _reject_suspect_url_scheme(tag: str, attr: str, value: str) -> str | None:
    """``attribute_filter`` callback for :func:`sanitize_html`.

    ``nh3``'s own ``url_schemes`` allow-list only rejects a value it recognises
    as having an ASCII scheme outside the set; a non-ASCII look-alike scheme
    (e.g. Cyrillic je for the 'j' of ``javascript:``) isn't parsed as a scheme
    at all, so it sails through unfiltered — the same bypass class
    :func:`html_safety_findings`'s ``_scheme_is_suspect`` already detects.
    Reusing that check here closes the gap for the attribute nh3 would
    otherwise keep verbatim.
    """
    if attr in ("href", "src"):
        scheme = _url_scheme(value)
        if scheme is not None and _scheme_is_suspect(scheme):
            return None
    return value


def sanitize_html(text: str) -> str:
    """Strip *text* down to the Story 10.35 / SEC-M2-sanitizer Approach 2
    allow-list (product sign-off, Trello card 4vEAwQWo) — this is the actual
    sanitizer, called before the Shopify write. Contrast with
    :func:`html_safety_findings`, which is advisory-only detection against a
    more permissive baseline and strips nothing.
    """
    return nh3.clean(
        text,
        tags=set(_ALLOWED_TAGS),
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=set(_ALLOWED_URL_SCHEMES),
        filter_style_properties=set(_ALLOWED_STYLE_PROPERTIES),
        attribute_filter=_reject_suspect_url_scheme,
    )


def html_strip_report(text: str) -> list[str]:
    """Human-readable list of tags/attributes :func:`sanitize_html` removes
    from *text*, for the write-preview "what will be stripped" diff. Only
    actual content loss is reported — safety additions nh3 makes (e.g.
    ``rel="noopener noreferrer"`` on ``<a>``) are not, since nothing was
    stripped.
    """
    sanitized = sanitize_html(text)
    if sanitized == text:
        return []

    before = _index_html(text)
    after = _index_html(sanitized)
    report: list[str] = []
    for tag, names in before.tag_attrs.items():
        if tag not in after.tag_attrs:
            report.append(f"<{tag}> (tag stripped by sanitizer)")
            continue
        for name in sorted(names - after.tag_attrs[tag]):
            report.append(f"{name}= on <{tag}> (attribute stripped by sanitizer)")
    return report


def filter_variant_targets(
    variant_ids: list[str] | None,
    variants: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Resolve caller-supplied `variant_ids` against a product's `variants` list.
    Single pass preserves caller order on both sides and dedupes both sides.
    Returns (targets, unresolved):
      - targets: list[variant dict] in caller's order, unknown ids skipped
      - unresolved: list[str] of ids the caller passed that weren't found
    When variant_ids is falsy, returns (list(variants), []) — default-to-all.
    """
    variants = variants or []
    if not variant_ids:
        return list(variants), []

    by_gid = {v["id"]: v for v in variants}
    targets = []
    seen_target_gids: set = set()
    unresolved: list[str] = []
    seen_unresolved: set = set()
    for vid in variant_ids:
        gid = to_gid("ProductVariant", vid)
        if gid in by_gid:
            if gid not in seen_target_gids:
                targets.append(by_gid[gid])
                seen_target_gids.add(gid)
        elif vid not in seen_unresolved:
            unresolved.append(vid)
            seen_unresolved.add(vid)
    return targets, unresolved
