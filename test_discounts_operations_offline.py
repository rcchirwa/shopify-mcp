"""
Offline unit tests for shopify.operations.discounts.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the discounts operations are callable from non-MCP
entry points (Story 10.27 / A5, AC4). discounts has no by-id/by-handle read pair
and no duplicated selection set, so no shared GraphQL fragment is extracted
(AC3 — the "no forced fragment" decision is pinned by test_no_shared_fragment).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_discounts_operations_offline.py -v
"""

from _testing import FakeClient
from shopify.operations import discounts as ops
from shopify.queries import discounts as q

# ---------- AC3: no shared fragment applies to discounts ----------


def test_no_shared_fragment_in_discount_queries():
    """discounts has no by-id/by-handle pair and no duplicated selection set, so
    none of its GraphQL strings declares or spreads a fragment."""
    for query in (q.GET_PRICE_RULES, q.CREATE_PRICE_RULE, q.CREATE_DISCOUNT_CODE):
        assert "fragment " not in query
        assert "..." not in query


# ---------- read operations (build vars + execute, return node list) ----------


def test_read_price_rules_returns_nodes_and_uses_fixed_page_size():
    fc = FakeClient([{"priceRules": {"nodes": [{"id": "g", "title": "Spring"}]}}])
    out = ops.read_price_rules(fc)
    assert out == [{"id": "g", "title": "Spring"}]
    assert fc.calls[0][0] == q.GET_PRICE_RULES
    assert fc.calls[0][1] == {"first": ops.PRICE_RULES_PAGE_SIZE}
    assert ops.PRICE_RULES_PAGE_SIZE == 50


def test_read_price_rules_empty_returns_empty_list():
    fc = FakeClient([{"priceRules": {"nodes": []}}])
    assert ops.read_price_rules(fc) == []


def test_read_price_rules_missing_connection_returns_empty_list():
    """Defensive: a permissions-trimmed / shape-drifted response with no
    priceRules connection yields an empty list rather than raising."""
    fc = FakeClient([{}])
    assert ops.read_price_rules(fc) == []


# ---------- write operations (build input + execute, return raw result) -------


def test_create_price_rule_wraps_input_and_returns_raw_result():
    raw = {"priceRuleCreate": {"priceRule": {"id": "gid://shopify/PriceRule/1"}}}
    fc = FakeClient([raw])
    price_rule_input = {"title": "T", "valueType": "PERCENTAGE", "value": "-20"}
    out = ops.create_price_rule(fc, price_rule_input)
    assert out is raw
    assert fc.calls[0][0] == q.CREATE_PRICE_RULE
    assert fc.calls[0][1] == {"input": price_rule_input}


def test_create_discount_code_builds_vars_and_returns_raw_result():
    raw = {"priceRuleDiscountCodeCreate": {"priceRuleDiscountCode": {"code": "LAUNCH20"}}}
    fc = FakeClient([raw])
    out = ops.create_discount_code(fc, "gid://shopify/PriceRule/1", "LAUNCH20")
    assert out is raw
    assert fc.calls[0][0] == q.CREATE_DISCOUNT_CODE
    assert fc.calls[0][1] == {
        "priceRuleId": "gid://shopify/PriceRule/1",
        "code": "LAUNCH20",
    }
