"""
Shared filtering helpers used by multiple tool modules.
"""

import re
from typing import Any

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
