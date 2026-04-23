"""
Offline unit tests for validators/naming.py.

Covers validate_title, validate_title_diff, format_validation_result,
and format_validation_diff. Pure-function tests — no Shopify client, no
.env required.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_naming_offline.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from validators.naming import (
    validate_title,
    validate_title_diff,
    format_validation_result,
    format_validation_diff,
)


COMPLIANT_AON = "All or Nothing | Spring Drop – Hoodie"
COMPLIANT_VANISH = "Vanish | Q1 Drop – Tee"


# ---- validate_title ----

def test_validate_compliant_aon_title_returns_empty():
    assert validate_title(COMPLIANT_AON) == []


def test_validate_compliant_vanish_title_returns_empty():
    assert validate_title(COMPLIANT_VANISH) == []


def test_validate_flags_missing_prefix():
    issues = validate_title("GSS Hoodie – Black")
    assert any("must start with" in i for i in issues)


def test_validate_flags_banned_parent_brand():
    issues = validate_title("All or Nothing | Global Streetwear Syndicate Drop – Tee")
    assert any("Global Streetwear Syndicate" in i for i in issues)


def test_validate_flags_missing_en_dash():
    issues = validate_title("All or Nothing | Spring - Hoodie")
    assert any("en dash" in i for i in issues)


def test_validate_flags_quoted_words():
    issues = validate_title('All or Nothing | "Iconic" Drop – Hoodie')
    assert any("quoted" in i for i in issues)


# ---- validate_title_diff ----

def test_diff_compliant_to_compliant_returns_empty_buckets():
    diff = validate_title_diff(COMPLIANT_AON, COMPLIANT_VANISH)
    assert diff == {"introduced": [], "pre_existing": [], "fixed": []}


def test_diff_compliant_to_broken_reports_introduced_only():
    diff = validate_title_diff(COMPLIANT_AON, "broken title")
    assert len(diff["introduced"]) > 0
    assert diff["pre_existing"] == []
    assert diff["fixed"] == []


def test_diff_broken_to_compliant_reports_fixed_only():
    diff = validate_title_diff("broken title", COMPLIANT_AON)
    assert diff["introduced"] == []
    assert diff["pre_existing"] == []
    assert len(diff["fixed"]) > 0


def test_diff_broken_to_broken_same_issues_are_pre_existing():
    old = "broken old"
    new = "broken new"  # same prefix + en-dash issues
    diff = validate_title_diff(old, new)
    assert diff["introduced"] == []
    assert len(diff["pre_existing"]) > 0
    assert diff["fixed"] == []


def test_diff_partially_fixed_reports_each_bucket():
    # Old has: missing prefix + no en dash. New fixes the prefix but still
    # has no en dash.
    old = "bad old title"
    new = "All or Nothing | bad new title"
    diff = validate_title_diff(old, new)
    # "missing prefix" should be in fixed (was in old, not in new).
    assert any("must start with" in i for i in diff["fixed"])
    # "no en dash" should be in pre_existing (in both).
    assert any("en dash" in i for i in diff["pre_existing"])


def test_diff_none_old_title_treats_all_new_issues_as_introduced():
    """New-product flow: no old title to diff against."""
    diff = validate_title_diff(None, "broken new")
    assert len(diff["introduced"]) > 0
    assert diff["pre_existing"] == []
    assert diff["fixed"] == []


def test_diff_empty_old_title_treats_all_new_issues_as_introduced():
    """Empty string old title should behave the same as None."""
    diff = validate_title_diff("", "broken new")
    assert len(diff["introduced"]) > 0
    assert diff["pre_existing"] == []
    assert diff["fixed"] == []


def test_diff_none_old_with_compliant_new_is_fully_clean():
    diff = validate_title_diff(None, COMPLIANT_AON)
    assert diff == {"introduced": [], "pre_existing": [], "fixed": []}


# ---- format_validation_result ----

def test_format_result_compliant_renders_check():
    out = format_validation_result(COMPLIANT_AON)
    assert out.startswith("✓ Title is compliant:")
    assert COMPLIANT_AON in out


def test_format_result_non_compliant_lists_each_issue():
    out = format_validation_result("broken")
    assert out.startswith("✗ Non-compliant title:")
    assert "•" in out


# ---- format_validation_diff ----

def test_format_diff_compliant_returns_check():
    out = format_validation_diff(COMPLIANT_AON, COMPLIANT_VANISH)
    assert out.startswith("✓ Title is compliant:")


def test_format_diff_introduced_rendered_under_introduced_header():
    out = format_validation_diff(COMPLIANT_AON, "broken new")
    assert "Introduced by this edit" in out
    # Shouldn't render empty headers for the other two buckets.
    assert "Pre-existing" not in out
    assert "Fixed by this edit" not in out


def test_format_diff_fixed_rendered_under_fixed_header():
    out = format_validation_diff("broken old", COMPLIANT_AON)
    assert "Fixed by this edit" in out
    assert "Introduced" not in out


def test_format_diff_pre_existing_only_prints_note():
    """When the edit doesn't introduce any issues but pre-existing ones remain,
    the operator should see the reassuring note — otherwise they might think
    their edit broke something."""
    out = format_validation_diff("broken title", "still broken title")
    assert "doesn't introduce new issues" in out
    assert "Pre-existing" in out


def test_format_diff_none_old_title_renders_introduced_only():
    out = format_validation_diff(None, "broken new")
    assert "Introduced by this edit" in out
    assert "Pre-existing" not in out


def test_format_diff_empty_old_title_renders_introduced_only():
    out = format_validation_diff("", "broken new")
    assert "Introduced by this edit" in out
    assert "Pre-existing" not in out


def test_format_diff_order_preserved_within_buckets():
    """Issue order within a bucket should match validate_title() output order
    so the rendering is deterministic."""
    old = None
    new = "broken Global Streetwear Syndicate title"  # triggers prefix + brand
    out = format_validation_diff(old, new)
    # "must start with" issue comes before "must never contain" in
    # validate_title()'s emission order.
    prefix_pos = out.find("must start with")
    brand_pos = out.find("must never contain")
    assert 0 <= prefix_pos < brand_pos, out
