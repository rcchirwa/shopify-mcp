"""
Offline unit tests for tools/discounts.py.

Covers the read-only get_discount_codes listing and the two-step create_
discount_code write path (price_rule create → code attach). No Shopify API
calls or .env required.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_discounts.py -v
"""

import re

import pytest

from shopify_mcp.tools import discounts
from shopify_mcp.tools.discounts import (
    CREATE_DISCOUNT_CODE,
    CREATE_PRICE_RULE,
    GET_CODE_DISCOUNTS,
)
from tests.support import CapturingServer, FakeClient


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    """Keep tests from polluting aon_mcp_log.txt."""
    monkeypatch.setattr(discounts, "log_write", lambda *a, **k: None)


def _build(responses):
    srv = CapturingServer()
    fc = FakeClient(responses)
    discounts.register(srv, fc)
    return srv.tools, fc


# ---- Fixture builders ----


def _discount_node(
    gid,
    title,
    status="ACTIVE",
    codes=None,
    codes_has_next=False,
    percentage=None,
    amount=None,
    usage_limit=None,
    ends_at=None,
    typename="DiscountCodeBasic",
):
    discount = {
        "__typename": typename,
        "title": title,
        "status": status,
        "endsAt": ends_at,
        "usageLimit": usage_limit,
        "codes": {
            "nodes": [{"code": c} for c in (codes if codes is not None else [title])],
            "pageInfo": {"hasNextPage": codes_has_next},
        },
    }
    if percentage is not None:
        discount["customerGets"] = {
            "value": {"__typename": "DiscountPercentage", "percentage": percentage}
        }
    elif amount is not None:
        discount["customerGets"] = {
            "value": {"__typename": "DiscountAmount", "amount": {"amount": amount}}
        }
    return {"id": f"gid://shopify/DiscountCodeNode/{gid}", "discount": discount}


def _price_rule_create_ok(rid="5001"):
    return {
        "priceRuleCreate": {
            "priceRule": {"id": f"gid://shopify/PriceRule/{rid}"},
            "priceRuleUserErrors": [],
        }
    }


def _price_rule_create_err(field, message):
    return {
        "priceRuleCreate": {
            "priceRule": None,
            "priceRuleUserErrors": [{"field": field, "message": message}],
        }
    }


def _discount_code_create_ok(code):
    return {
        "priceRuleDiscountCodeCreate": {
            "priceRuleDiscountCode": {"code": code},
            "userErrors": [],
        }
    }


def _discount_code_create_err(field, message):
    return {
        "priceRuleDiscountCodeCreate": {
            "priceRuleDiscountCode": None,
            "userErrors": [{"field": field, "message": message}],
        }
    }


# ---- get_discount_codes ----


def test_get_discount_codes_empty_returns_no_codes_found():
    tools, fc = _build([{"discountNodes": {"nodes": []}}])
    out = tools["get_discount_codes"]()
    assert out == "No discount codes found."
    assert fc.calls[0][0] == GET_CODE_DISCOUNTS
    assert fc.calls[0][1] == {"first": 50, "query": "method:code", "codesFirst": 10}


def test_get_discount_codes_renders_each_discount_with_codes_value_limit_expiry():
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Spring Sale",
                            codes=["SPRING25"],
                            percentage=0.25,
                            usage_limit=100,
                            ends_at="2026-06-30T23:59:59Z",
                        ),
                        _discount_node("5002", "VIP Perk", codes=["VIP10"], percentage=0.10),
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Discount codes (2 found):" in out
    assert "[5001] Spring Sale" in out
    assert "Codes: SPRING25" in out and "25% off" in out
    assert "Usage limit: 100" in out
    assert "Ends: 2026-06-30T23:59:59Z" in out
    assert "[5002] VIP Perk" in out
    assert "Codes: VIP10" in out and "10% off" in out


def test_get_discount_codes_unlimited_when_usage_limit_is_null():
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node("5001", "Evergreen", usage_limit=None),
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Usage limit: unlimited" in out


def test_get_discount_codes_no_expiry_when_ends_at_is_null():
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node("5001", "Evergreen", ends_at=None),
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Ends: no expiry" in out


def test_get_discount_codes_joins_multiple_redeem_codes_with_comma():
    """A bulk-code discount can have more than one redeem code under one title."""
    tools, fc = _build(
        [{"discountNodes": {"nodes": [_discount_node("5001", "Bulk", codes=["A", "B", "C"])]}}]
    )
    out = tools["get_discount_codes"]()
    assert "Codes: A, B, C" in out


def test_get_discount_codes_non_percentage_discount_shows_kind_instead_of_value():
    """DiscountCodeApp/Bxgy/FreeShipping have no customerGets.value — the value
    line falls back to the GraphQL type name rather than crashing."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001", "Free Ship Weekend", typename="DiscountCodeFreeShipping"
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "DiscountCodeFreeShipping" in out


def test_get_discount_codes_fixed_amount_shows_dollar_value():
    """A DiscountCodeBasic whose customerGets.value is a DiscountAmount (a
    "$10 off" code, not a percentage) must show the dollar value — not fall
    through to the generic __typename branch."""
    tools, fc = _build(
        [{"discountNodes": {"nodes": [_discount_node("5001", "Ten Off", amount="10.00")]}}]
    )
    out = tools["get_discount_codes"]()
    assert "$10.00 off" in out
    assert "DiscountCodeBasic" not in out


def test_get_discount_codes_notes_when_redeem_codes_are_capped():
    """A bulk-code discount with more codes than the fixed page fetches gets a
    visible note rather than silently showing a truncated list as complete."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [_discount_node("5001", "Bulk", codes=["A", "B"], codes_has_next=True)]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Codes: A, B (+more not shown)" in out


def test_get_discount_codes_handles_null_codes_nodes_defensively():
    """Defensive: a permissions-trimmed / shape-drifted response can return
    codes.nodes=null (present but null, not just absent) — must not crash."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        {
                            "id": "gid://shopify/DiscountCodeNode/5001",
                            "discount": {
                                "__typename": "DiscountCodeBasic",
                                "title": "Drifted",
                                "status": "ACTIVE",
                                "codes": {"nodes": None},
                            },
                        }
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Codes: (no code)" in out


# ---- create_discount_code — preview ----


def test_create_discount_code_preview_does_not_mutate():
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="Launch Drop",
        code="LAUNCH20",
        percentage_off=20,
        confirm=False,
    )
    assert "PREVIEW — New discount code" in out
    assert "Title         : Launch Drop" in out
    assert "Code          : LAUNCH20" in out
    assert "Discount      : 20% off" in out
    assert "Usage limit   : unlimited" in out
    assert "To apply, call again with confirm=True." in out
    assert len(fc.calls) == 0, "preview must not issue any Shopify calls"


def test_create_discount_code_preview_shows_usage_limit_when_set():
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="Capped",
        code="CAPPED",
        percentage_off=15,
        usage_limit=500,
        confirm=False,
    )
    assert "Usage limit   : 500" in out


# ---- create_discount_code — confirm (happy path) ----


def test_create_discount_code_masks_code_in_audit_log(monkeypatch):
    # SEC-12: the plaintext discount code must not be persisted to the local
    # audit log. The immediate tool response may still echo it (the caller
    # supplied it), but the durable log line masks it.
    captured = {}
    monkeypatch.setattr(
        discounts,
        "log_write",
        lambda name, desc: captured.update(name=name, desc=desc),
    )
    tools, fc = _build(
        [
            _price_rule_create_ok(rid="5001"),
            _discount_code_create_ok("LAUNCH20"),
        ]
    )
    out = tools["create_discount_code"](
        title="Launch Drop",
        code="LAUNCH20",
        percentage_off=20,
        confirm=True,
    )
    assert out.startswith("Done.")
    assert "LAUNCH20" not in captured["desc"]
    assert "code=***" in captured["desc"]


def test_create_discount_code_confirmed_issues_two_mutations_in_order():
    tools, fc = _build(
        [
            _price_rule_create_ok(rid="5001"),
            _discount_code_create_ok("LAUNCH20"),
        ]
    )
    out = tools["create_discount_code"](
        title="Launch Drop",
        code="LAUNCH20",
        percentage_off=20,
        confirm=True,
    )
    assert out.startswith("Done.")
    assert "Price rule id=5001 created." in out
    assert len(fc.calls) == 2
    assert fc.calls[0][0] == CREATE_PRICE_RULE
    assert fc.calls[1][0] == CREATE_DISCOUNT_CODE
    # Second call references the rule id from the first response.
    assert fc.calls[1][1] == {
        "priceRuleId": "gid://shopify/PriceRule/5001",
        "code": "LAUNCH20",
    }


def test_create_discount_code_negates_percentage_on_write():
    """Shopify wants a negative value for percentage-off — the tool normalizes
    positive inputs by flipping the sign."""
    tools, fc = _build(
        [
            _price_rule_create_ok(),
            _discount_code_create_ok("X"),
        ]
    )
    tools["create_discount_code"](
        title="Test",
        code="X",
        percentage_off=20,
        confirm=True,
    )
    rule_input = fc.calls[0][1]["input"]
    assert rule_input["value"] == "-20"


def test_create_discount_code_usage_limit_zero_omits_key():
    """usage_limit=0 means unlimited — the PriceRuleInput should NOT include a
    usageLimit key (Shopify interprets null as unlimited but a 0 as invalid)."""
    tools, fc = _build(
        [
            _price_rule_create_ok(),
            _discount_code_create_ok("X"),
        ]
    )
    tools["create_discount_code"](
        title="Unlim",
        code="X",
        percentage_off=10,
        usage_limit=0,
        confirm=True,
    )
    rule_input = fc.calls[0][1]["input"]
    assert "usageLimit" not in rule_input


def test_create_discount_code_usage_limit_positive_included():
    tools, fc = _build(
        [
            _price_rule_create_ok(),
            _discount_code_create_ok("X"),
        ]
    )
    tools["create_discount_code"](
        title="Capped",
        code="X",
        percentage_off=10,
        usage_limit=500,
        confirm=True,
    )
    assert fc.calls[0][1]["input"]["usageLimit"] == 500


def test_create_discount_code_starts_at_is_iso8601_z():
    """startsAt must be a Shopify-accepted ISO-8601 UTC string."""
    tools, fc = _build(
        [
            _price_rule_create_ok(),
            _discount_code_create_ok("X"),
        ]
    )
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        confirm=True,
    )
    starts_at = fc.calls[0][1]["input"]["startsAt"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", starts_at), starts_at


def test_create_discount_code_price_rule_input_shape():
    """PriceRuleInput must have the fixed-shape fields Shopify requires."""
    tools, fc = _build(
        [
            _price_rule_create_ok(),
            _discount_code_create_ok("X"),
        ]
    )
    tools["create_discount_code"](
        title="Shape Check",
        code="X",
        percentage_off=20,
        confirm=True,
    )
    rule_input = fc.calls[0][1]["input"]
    assert rule_input["title"] == "Shape Check"
    assert rule_input["target"] == "LINE_ITEM"
    assert rule_input["allocationMethod"] == "ACROSS"
    assert rule_input["valueType"] == "PERCENTAGE"
    assert rule_input["customerSelection"] == {"forAllCustomers": True}


# ---- create_discount_code — error paths ----


def test_create_discount_code_halts_before_code_creation_on_price_rule_error():
    """priceRuleUserErrors on step 1 must short-circuit — no code creation call."""
    tools, fc = _build(
        [
            _price_rule_create_err(["input", "title"], "Title has already been used"),
        ]
    )
    out = tools["create_discount_code"](
        title="Dup",
        code="X",
        percentage_off=10,
        confirm=True,
    )
    assert out.startswith("Error creating price rule:")
    assert "Title has already been used" in out
    assert len(fc.calls) == 1, "code-create step must not run after rule-create fails"


def test_create_discount_code_surfaces_code_attachment_user_errors():
    tools, fc = _build(
        [
            _price_rule_create_ok(),
            _discount_code_create_err(["code"], "Code has already been taken"),
        ]
    )
    out = tools["create_discount_code"](
        title="T",
        code="DUPE",
        percentage_off=10,
        confirm=True,
    )
    assert out.startswith("Error attaching discount code:")
    assert "Code has already been taken" in out


# ---- create_discount_code — percentage_off boundary ----


@pytest.mark.parametrize("percentage_off", [0, -5, 100.01])
def test_create_discount_code_rejects_out_of_range_percentage(percentage_off):
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="Bad",
        code="BAD",
        percentage_off=percentage_off,
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "percentage_off" in out
    assert len(fc.calls) == 0, "out-of-range percentage_off must reject before any Shopify call"


@pytest.mark.parametrize("percentage_off", [1, 20, 100])
def test_create_discount_code_accepts_boundary_and_typical_percentages(percentage_off):
    tools, fc = _build(
        [
            _price_rule_create_ok(),
            _discount_code_create_ok("X"),
        ]
    )
    out = tools["create_discount_code"](
        title="Good",
        code="GOOD",
        percentage_off=percentage_off,
        confirm=True,
    )
    assert out.startswith("Done.")


def test_create_discount_code_handles_missing_rule_id_defensively():
    """If the priceRuleCreate payload has no id (shape drift, partial response),
    don't attempt the second step with None — surface a clear error."""
    tools, fc = _build(
        [
            {
                "priceRuleCreate": {
                    "priceRule": None,
                    "priceRuleUserErrors": [],
                }
            }
        ]
    )
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        confirm=True,
    )
    assert "price rule created but no ID returned" in out
    assert len(fc.calls) == 1
