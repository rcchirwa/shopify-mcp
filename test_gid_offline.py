"""
Offline unit tests for tools._gid (to_gid / from_gid).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_gid_offline.py -v
"""

from typing import Any

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


def test_from_gid_tolerates_none_via_any_payload() -> None:
    # from_gid's param was narrowed to `str` (Story 10.33 / Q6) so mypy flags
    # any statically-typed `str | None` caller. But the runtime `if not gid`
    # guard is deliberately retained, NOT dead code: real None values arrive
    # through `dict[str, Any]` GraphQL payloads (Shopify returns `id: null` on
    # partial / permissions-trimmed fields), reaching from_gid as `Any` —
    # invisible to mypy. This pins the guard so it survives the type narrowing;
    # drop it and `None.split(...)` raises AttributeError right here.
    payload: dict[str, Any] = {"id": None}
    assert from_gid(payload.get("id", "")) == ""


def test_from_gid_tolerates_empty_string() -> None:
    assert from_gid("") == ""
