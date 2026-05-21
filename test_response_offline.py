"""
Offline unit tests for tools._response (with_confirm_hint / extract_user_errors /
format_user_errors_joined / format_user_errors).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_response_offline.py -v
"""

from tools._response import (
    extract_user_errors,
    format_user_errors,
    format_user_errors_joined,
    with_confirm_hint,
)

# ---------- with_confirm_hint ----------


def test_with_confirm_hint_appends_exact_contract_string() -> None:
    # The tail is asserted verbatim by tool-level tests (test_inventory_offline,
    # test_discounts_offline). Pin it here too so drift is caught at the source.
    assert with_confirm_hint("PREVIEW — Something") == (
        "PREVIEW — Something\n\nTo apply, call again with confirm=True."
    )


def test_with_confirm_hint_on_empty_preview() -> None:
    assert with_confirm_hint("") == "\n\nTo apply, call again with confirm=True."


# ---------- extract_user_errors ----------


def test_extract_user_errors_returns_list() -> None:
    errors = [{"field": "title", "message": "blank"}]
    result = {"productUpdate": {"userErrors": errors}}
    assert extract_user_errors(result, "productUpdate") == errors


def test_extract_user_errors_missing_mutation_returns_empty_list() -> None:
    assert extract_user_errors({}, "productUpdate") == []


def test_extract_user_errors_null_mutation_returns_empty_list() -> None:
    # Shopify can return explicit null for a mutation payload.
    assert extract_user_errors({"productUpdate": None}, "productUpdate") == []


def test_extract_user_errors_null_list_returns_empty_list() -> None:
    # Mutation returned, userErrors slot is explicit null rather than [].
    assert extract_user_errors({"productUpdate": {"userErrors": None}}, "productUpdate") == []


def test_extract_user_errors_alt_error_key() -> None:
    result = {"publishablePublish": {"mediaUserErrors": [{"field": "media", "message": "bad"}]}}
    assert extract_user_errors(result, "publishablePublish", error_key="mediaUserErrors") == [
        {"field": "media", "message": "bad"}
    ]


# ---------- format_user_errors_joined ----------


def test_format_user_errors_joined_happy_path() -> None:
    # Same payload as format_user_errors — but no "Error: " prefix. Used by
    # bulk-op summaries that embed the formatted string inside a bullet row.
    result = {
        "productUpdate": {
            "userErrors": [
                {"field": "title", "message": "can't be blank"},
                {"field": "handle", "message": "already taken"},
            ]
        }
    }
    assert format_user_errors_joined(result, "productUpdate") == (
        "title: can't be blank; handle: already taken"
    )


def test_format_user_errors_joined_no_errors_returns_none() -> None:
    assert format_user_errors_joined({"productUpdate": {"userErrors": []}}, "productUpdate") is None


def test_format_user_errors_joined_missing_mutation_key_returns_none() -> None:
    assert format_user_errors_joined({}, "productUpdate") is None


def test_format_user_errors_joined_missing_user_errors_slot_returns_none() -> None:
    # Mutation returned, but the userErrors key was omitted entirely.
    assert format_user_errors_joined({"productUpdate": {}}, "productUpdate") is None


def test_format_user_errors_joined_mutation_slot_is_none_returns_none() -> None:
    assert format_user_errors_joined({"productUpdate": None}, "productUpdate") is None


def test_format_user_errors_joined_alt_error_key() -> None:
    result = {
        "priceRuleCreate": {"priceRuleUserErrors": [{"field": "value", "message": "out of range"}]}
    }
    assert (
        format_user_errors_joined(result, "priceRuleCreate", error_key="priceRuleUserErrors")
        == "value: out of range"
    )


def test_format_user_errors_joined_tolerates_missing_field_or_message() -> None:
    result: dict = {"productUpdate": {"userErrors": [{}]}}
    assert format_user_errors_joined(result, "productUpdate") == "None: None"


# ---------- format_user_errors ----------


def test_format_user_errors_happy_path() -> None:
    result = {
        "productUpdate": {
            "userErrors": [
                {"field": "title", "message": "can't be blank"},
                {"field": "handle", "message": "already taken"},
            ]
        }
    }
    assert format_user_errors(result, "productUpdate") == (
        "Error: title: can't be blank; handle: already taken"
    )


def test_format_user_errors_no_errors_returns_none() -> None:
    result: dict = {"productUpdate": {"userErrors": []}}
    assert format_user_errors(result, "productUpdate") is None


def test_format_user_errors_missing_mutation_key_returns_none() -> None:
    # Whole mutation slot absent (e.g. partial/permissions-trimmed response).
    assert format_user_errors({}, "productUpdate") is None


def test_format_user_errors_missing_user_errors_slot_returns_none() -> None:
    # Mutation returned, but the userErrors key was omitted entirely.
    assert format_user_errors({"productUpdate": {}}, "productUpdate") is None


def test_format_user_errors_mutation_slot_is_none_returns_none() -> None:
    # GraphQL can return explicit null for a mutation payload — `or {}` guard.
    assert format_user_errors({"productUpdate": None}, "productUpdate") is None


def test_format_user_errors_alt_error_key() -> None:
    # priceRuleCreate uses priceRuleUserErrors instead of userErrors.
    result = {
        "priceRuleCreate": {"priceRuleUserErrors": [{"field": "value", "message": "out of range"}]}
    }
    assert (
        format_user_errors(result, "priceRuleCreate", error_key="priceRuleUserErrors")
        == "Error: value: out of range"
    )


def test_format_user_errors_custom_prefix() -> None:
    result = {
        "priceRuleDiscountCodeCreate": {
            "userErrors": [{"field": "code", "message": "already exists"}]
        }
    }
    assert (
        format_user_errors(
            result, "priceRuleDiscountCodeCreate", prefix="Error attaching discount code"
        )
        == "Error attaching discount code: code: already exists"
    )


def test_format_user_errors_tolerates_missing_field_or_message() -> None:
    # Defensive: Shopify's contract guarantees both keys, but an unexpected
    # response shape yields "None: None" rather than a KeyError.
    result: dict = {"productUpdate": {"userErrors": [{}]}}
    assert format_user_errors(result, "productUpdate") == "Error: None: None"
