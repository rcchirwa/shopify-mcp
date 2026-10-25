"""
Offline unit tests for tools._gid (to_gid / from_gid).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_gid.py -v
"""

from typing import Any

import pytest

from shopify_mcp.shopify._ids import product_gid, typed_gid
from shopify_mcp.tools._gid import from_gid, to_gid

# ---------- to_gid ----------


def test_to_gid_builds_correct_gid_from_int() -> None:
    assert to_gid("Product", 123) == "gid://shopify/Product/123"


def test_to_gid_builds_correct_gid_from_str() -> None:
    assert to_gid("Product", "123") == "gid://shopify/Product/123"


def test_to_gid_uses_resource_type() -> None:
    assert to_gid("ProductVariant", 42) == "gid://shopify/ProductVariant/42"


# ---------- product_gid (Story 10.108) ----------


@pytest.mark.parametrize("value", [123, "123", "gid://shopify/Product/123"])
def test_product_gid_accepts_a_numeric_id_or_a_product_gid(value: Any) -> None:
    assert product_gid(value) == "gid://shopify/Product/123"


def test_product_gid_refuses_another_type_without_echoing_it() -> None:
    with pytest.raises(ValueError) as exc:
        product_gid("gid://shopify/ProductVariant/987")
    assert "Product GID" in str(exc.value)
    assert "987" not in str(exc.value)


# ---------- typed_gid (Story 10.109) ----------


@pytest.mark.parametrize(
    "value", [42, "42", " 42 ", "gid://shopify/Order/42", " gid://shopify/Order/42"]
)
def test_typed_gid_accepts_a_numeric_id_or_a_gid_of_that_type(value: Any) -> None:
    assert typed_gid("Order", value, "bad order") == "gid://shopify/Order/42"


@pytest.mark.parametrize(
    "value",
    [
        "gid://shopify/ProductVariant/42",
        " gid://shopify/ProductVariant/42",
        "gid://shopify/Order/",
        "foo/42",
        "gid://shopify/OrderX/42",
    ],
)
def test_typed_gid_refuses_another_type_an_empty_body_or_a_path(value: str) -> None:
    with pytest.raises(ValueError) as exc:
        typed_gid("Order", value, "bad order")
    assert str(exc.value) == "bad order"


def test_product_gid_uses_the_shared_rule_for_a_path() -> None:
    with pytest.raises(ValueError):
        product_gid("foo/42")


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
