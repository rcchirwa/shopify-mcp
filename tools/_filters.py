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


# URL-bearing attributes whose value carries a scheme worth vetting against
# nh3's allow-list. Kept deliberately small — these are the attributes a
# storefront theme renders as a navigable / loadable URL.
_URL_ATTRS = frozenset(
    {"href", "src", "xlink:href", "action", "formaction", "poster", "background", "cite"}
)


def _url_scheme(value: str) -> str | None:
    """Return the scheme of *value* (text before the first ``:``) when it looks
    like a real ``scheme:`` prefix, else ``None``.

    A path / fragment / query / whitespace character before the colon means
    there is no scheme — it is a relative URL like ``/a:b`` or ``#a:b``.
    """
    colon = value.find(":")
    if colon <= 0:
        return None
    head = value[:colon]
    if any(c in head for c in "/?# \t\r\n"):
        return None
    return head


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
       every tag / attribute the sanitizer would strip. Catches ``<style>`` / CSS
       injection and attribute injection that the curated blocklist never knew.
    2. **URL-scheme check** — vet URL-bearing attributes against
       ``nh3.ALLOWED_URL_SCHEMES``. Catches ``javascript:`` / ``data:`` and
       Unicode look-alike schemes (e.g. a Cyrillic je, U+0458, standing in for
       the 'j' of ``javascript:``) that nh3 itself leaves in place.

    Findings already named by the blocklist (a ``<tag`` prefix, an ``attr=``
    handler token, or a ``scheme:`` substring) are not repeated.
    """
    findings: list[str] = list(dangerous_html_patterns(text))
    seen: set[str] = set(findings)

    def _add(message: str) -> None:
        if message not in seen:
            seen.add(message)
            findings.append(message)

    before = _index_html(text)
    after = _index_html(nh3.clean(text))

    for tag, names in before.tag_attrs.items():
        if tag not in after.tag_attrs:
            # The blocklist may already name this tag (e.g. '<script'); only add
            # the ones it does not know (e.g. '<style>').
            if f"<{tag}" not in seen:
                _add(f"<{tag}> (tag stripped by sanitizer)")
            continue
        for name in sorted(names - after.tag_attrs[tag]):
            if name in _URL_ATTRS:
                continue  # URL attributes are covered by the scheme check below
            if f"{name}=" not in seen:  # on*=-handler tokens come from the blocklist
                _add(f"{name}= on <{tag}> (attribute stripped by sanitizer)")

    for tag, name, value in before.url_values:
        scheme = _url_scheme(value)
        if scheme is None:
            continue
        if scheme.casefold() in nh3.ALLOWED_URL_SCHEMES:
            continue
        # ASCII javascript:/vbscript:/data:text/html are already named by the
        # blocklist — don't double-report; look-alike schemes still surface.
        if any(token.startswith(f"{scheme.casefold()}:") for token in seen):
            continue
        _add(f"{name}= on <{tag}> uses disallowed URL scheme {scheme!r}")

    return findings


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
