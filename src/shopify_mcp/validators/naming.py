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
        # Message stays fully deterministic (no title interpolation) so that
        # validate_title_diff can classify the same violation across two
        # different titles as `pre_existing` rather than a pair of
        # `introduced` + `fixed` entries.
        warnings.append("Title must start with 'All or Nothing | ' or 'Vanish | '.")

    if BANNED_PARENT_BRAND in title:
        warnings.append(f"Title must never contain the parent brand name '{BANNED_PARENT_BRAND}'.")

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


def validate_title_diff(old_title: str, new_title: str) -> dict:
    """
    Compare validation results of old_title vs. new_title and categorize
    violations by their relationship to the edit.

    Returns a dict with three lists (any/all may be empty):
      - introduced:   in new but not in old (this edit broke them)
      - pre_existing: in both (already broken, unchanged by this edit)
      - fixed:        in old but not in new (this edit fixed them)

    When old_title is empty or None (new-product flow), every new issue is
    reported as 'introduced' — there's no pre-existing state to diff against.
    """
    new_issues = validate_title(new_title)
    old_issues = validate_title(old_title) if old_title else []

    old_set = set(old_issues)
    new_set = set(new_issues)

    # Preserve the order produced by validate_title() so callers get a stable,
    # human-predictable ordering (structural → content → format issues).
    introduced = [i for i in new_issues if i not in old_set]
    pre_existing = [i for i in new_issues if i in old_set]
    fixed = [i for i in old_issues if i not in new_set]

    return {
        "introduced": introduced,
        "pre_existing": pre_existing,
        "fixed": fixed,
    }


def format_validation_diff(old_title: str, new_title: str) -> str:
    """
    Multi-section validation output for update_product_title previews.
    Distinguishes violations introduced by THIS edit from pre-existing ones,
    so legacy non-compliant titles don't drown out the real signal.
    """
    diff = validate_title_diff(old_title, new_title)
    introduced = diff["introduced"]
    pre_existing = diff["pre_existing"]
    fixed = diff["fixed"]

    if not introduced and not pre_existing and not fixed:
        return f"✓ Title is compliant: {new_title}"

    lines = [f"Naming validation for: {new_title}"]

    if not introduced and pre_existing:
        lines.append(
            "  Note: this edit doesn't introduce new issues; pre-existing violations remain."
        )

    if introduced:
        lines.append(f"  Introduced by this edit ({len(introduced)}):")
        for issue in introduced:
            lines.append(f"    • {issue}")
    if pre_existing:
        lines.append(f"  Pre-existing ({len(pre_existing)} — not introduced by this edit):")
        for issue in pre_existing:
            lines.append(f"    • {issue}")
    if fixed:
        lines.append(f"  Fixed by this edit ({len(fixed)}):")
        for issue in fixed:
            lines.append(f"    • {issue}")

    return "\n".join(lines)
