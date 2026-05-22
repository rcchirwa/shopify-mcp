"""
Offline unit tests for tools._gid (to_gid / from_gid).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_gid_offline.py -v
"""

from tools._gid import from_gid, to_gid

# ---------- to_gid ----------


def test_to_gid_builds_correct_gid_from_int() -> None:
    assert to_gid("Product", 123) == "gid://shopify/Product/123"


def test_to_gid_builds_correct_gid_from_str() -> None:
    assert to_gid("Product", "123") == "gid://shopify/Product/123"


def test_to_gid_uses_resource_type() -> None:
    assert to_gid("ProductVariant", 42) == "gid://shopify/ProductVariant/42"


# ---------- from_gid ----------


def test_from_gid_extracts_trailing_numeric_id() -> None:
    assert from_gid("gid://shopify/Product/123") == "123"


def test_from_gid_tolerates_none() -> None:
    # Shopify can return `id: null` on partial / permissions-trimmed fields,
    # and callers that do `from_gid(obj.get("id", ""))` still pass None through
    # because .get only applies the default for missing keys. Must not crash.
    assert from_gid(None) == ""


def test_from_gid_tolerates_empty_string() -> None:
    assert from_gid("") == ""
