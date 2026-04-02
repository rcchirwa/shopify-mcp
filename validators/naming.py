"""
AON + Vanish product title naming convention validator.

AON format:  All or Nothing | [Drop Name] [Product Type] – [Variant]
Vanish format: Vanish | [Collection] [Product Type] – [Detail]

The en dash separator is: –  (U+2013, not a hyphen)
"""

import re


VALID_PREFIXES = ("All or Nothing | ", "Vanish | ")
EN_DASH_SEPARATOR = " – "
BANNED_PARENT_BRAND = "Global Streetwear Syndicate"


def validate_title(title: str) -> list[str]:
    """
    Validate a product title against AON/Vanish naming conventions.
    Returns a list of warning strings. Empty list means fully compliant.
    """
    warnings = []

    if not any(title.startswith(prefix) for prefix in VALID_PREFIXES):
        warnings.append(
            f"Title must start with 'All or Nothing | ' or 'Vanish | '. "
            f"Got: '{title[:30]}...'"
        )

    if BANNED_PARENT_BRAND in title:
        warnings.append(
            f"Title must never contain the parent brand name "
            f"'{BANNED_PARENT_BRAND}'."
        )

    if EN_DASH_SEPARATOR not in title:
        warnings.append(
            "Title must contain ' – ' (en dash with spaces) as the variant separator. "
            "Check for a regular hyphen '-' instead of an en dash '–'."
        )

    if re.search(r'"[^"]*"', title) or re.search(r"'[^']*'", title):
        warnings.append("Title must not contain quoted words.")

    return warnings


def format_validation_result(title: str) -> str:
    """Return a formatted validation message for a given title."""
    issues = validate_title(title)
    if not issues:
        return f"✓ Title is compliant: {title}"
    lines = [f"✗ Non-compliant title: {title}"]
    for issue in issues:
        lines.append(f"  • {issue}")
    return "\n".join(lines)
