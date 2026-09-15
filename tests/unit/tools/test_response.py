"""
Offline unit tests for tools._response (with_confirm_hint / extract_user_errors /
format_user_errors_joined / format_user_errors / format_field_path /
format_path_user_errors).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_response.py -v
"""

from shopify_mcp.tools._response import (
    extract_user_errors,
    format_field_path,
    format_path_user_errors,
    format_user_errors,
    format_user_errors_joined,
    with_confirm_hint,
)

# ---------- with_confirm_hint ----------


def test_with_confirm_hint_appends_exact_contract_string() -> None:
    # The tail is asserted verbatim by tool-level tests (tests/unit/tools/
    # test_inventory.py, test_discounts.py). Pin it here too so drift is
    # caught at the source.
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


# ---------- format_field_path ----------


def test_format_field_path_joins_multiple_segments_with_dots() -> None:
    # DiscountUserError.field is [String!] — a path, not a scalar.
    assert format_field_path({"field": ["basicCodeDiscount", "code"]}) == "basicCodeDiscount.code"


def test_format_field_path_single_segment_has_no_separator() -> None:
    assert format_field_path({"field": ["code"]}) == "code"


def test_format_field_path_empty_list_returns_empty_string() -> None:
    # Callers render the empty string as their own placeholder, so the
    # helper itself stays placeholder-free.
    assert format_field_path({"field": []}) == ""


def test_format_field_path_missing_field_key_returns_empty_string() -> None:
    assert format_field_path({"message": "something went wrong"}) == ""


def test_format_field_path_none_field_returns_empty_string() -> None:
    # Shopify can return explicit null for an error with no field context.
    assert format_field_path({"field": None}) == ""


def test_format_field_path_stringifies_non_string_segments() -> None:
    # productVariantsBulkUpdate paths carry a positional index; the schema
    # types it as a string, but str() keeps an int from raising.
    assert format_field_path({"field": ["variants", 0, "price"]}) == "variants.0.price"


# ---------- format_path_user_errors ----------


def test_format_path_user_errors_joins_errors_with_semicolons() -> None:
    errors = [
        {"field": ["metafields", "0", "value"], "message": "is invalid"},
        {"field": ["metafields", "1", "key"], "message": "is blank"},
    ]
    assert format_path_user_errors(errors) == (
        "metafields.0.value: is invalid; metafields.1.key: is blank"
    )


def test_format_path_user_errors_renders_no_field_placeholder() -> None:
    # An error with no field context must not render as ": message".
    assert format_path_user_errors([{"field": [], "message": "is invalid"}]) == (
        "(no field): is invalid"
    )


def test_format_path_user_errors_missing_message_renders_empty() -> None:
    # Defensive: matches the `e.get("message", "")` the nine inlined copies used.
    assert format_path_user_errors([{"field": ["code"]}]) == "code: "


def test_format_path_user_errors_empty_list_returns_empty_string() -> None:
    # Callers guard on the error list being non-empty before formatting, so
    # this is the degenerate case rather than a None-returning signal.
    assert format_path_user_errors([]) == ""
