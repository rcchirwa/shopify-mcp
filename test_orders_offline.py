"""
Offline unit tests for tools/orders.py.

Exercises the read-only get_orders and get_order tools with a scripted
FakeClient — no Shopify API calls or .env required. Covers rendering,
limit clamping, traffic-source fallbacks, and GID normalization.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_orders_offline.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from tools import orders
from tools.orders import GET_ORDERS, GET_ORDER_BY_ID
from _testing import CapturingServer, FakeClient


def _build(responses):
    srv = CapturingServer()
    fc = FakeClient(responses)
    orders.register(srv, fc)
    return srv.tools, fc


# ---- Fixture builders ----

def _order_node(
    oid="1001",
    name="#1001",
    created_at="2026-04-22T10:00:00Z",
    total="42.00",
    line_items=None,
    referring_site=None,
    landing_site=None,
    display_financial_status=None,
    display_fulfillment_status=None,
):
    node = {
        "id": f"gid://shopify/Order/{oid}",
        "name": name,
        "createdAt": created_at,
        "totalPriceSet": {"shopMoney": {"amount": total}},
        "lineItems": {"nodes": line_items or []},
        "referringSite": referring_site,
        "landingSite": landing_site,
    }
    if display_financial_status is not None:
        node["displayFinancialStatus"] = display_financial_status
    if display_fulfillment_status is not None:
        node["displayFulfillmentStatus"] = display_fulfillment_status
    return node


def _line_item(name, quantity, unit_price=None):
    li = {"name": name, "quantity": quantity}
    if unit_price is not None:
        li["originalUnitPriceSet"] = {"shopMoney": {"amount": unit_price}}
    return li


# ---- get_orders ----

def test_get_orders_empty_returns_no_orders_found():
    tools, fc = _build([{"orders": {"nodes": []}}])
    out = tools["get_orders"]()
    assert out == "No orders found."
    assert fc.calls[0][0] == GET_ORDERS
    assert fc.calls[0][1] == {"first": 20}


def test_get_orders_formats_each_order_with_line_items_and_source():
    tools, fc = _build([{"orders": {"nodes": [
        _order_node(
            oid="1001", total="100.00",
            line_items=[_line_item("Hoodie", 2)],
            referring_site="https://instagram.com/aoncypher",
        ),
        _order_node(
            oid="1002", total="42.50",
            line_items=[_line_item("Tee", 1), _line_item("Hat", 3)],
            landing_site="https://shop.example/drop",
        ),
    ]}}])
    out = tools["get_orders"]()
    assert "Recent orders (2):" in out
    assert "[1001]" in out and "$100.00" in out and "Hoodie x2" in out
    assert "instagram.com/aoncypher" in out
    assert "[1002]" in out and "Tee x1, Hat x3" in out
    assert "shop.example/drop" in out


def test_get_orders_limit_capped_at_250():
    tools, fc = _build([{"orders": {"nodes": []}}])
    tools["get_orders"](limit=500)
    assert fc.calls[0][1] == {"first": 250}


def test_get_orders_falls_back_to_landing_site_when_referring_site_missing():
    tools, fc = _build([{"orders": {"nodes": [
        _order_node(referring_site=None, landing_site="https://shop.example/launch"),
    ]}}])
    out = tools["get_orders"]()
    assert "https://shop.example/launch" in out


def test_get_orders_falls_back_to_direct_unknown_when_both_missing():
    tools, fc = _build([{"orders": {"nodes": [
        _order_node(referring_site=None, landing_site=None),
    ]}}])
    out = tools["get_orders"]()
    assert "direct / unknown" in out


def test_get_orders_handles_missing_total_without_crashing():
    """Defensive: if totalPriceSet.shopMoney.amount is missing, fall back to
    'N/A' rather than crash on a None.get() chain."""
    tools, fc = _build([{"orders": {"nodes": [{
        "id": "gid://shopify/Order/1001",
        "name": "#1001",
        "createdAt": "2026-04-22T10:00:00Z",
        "totalPriceSet": None,
        "lineItems": {"nodes": []},
    }]}}])
    out = tools["get_orders"]()
    assert "$N/A" in out


# ---- get_order ----

def test_get_order_not_found_returns_message():
    tools, fc = _build([{"order": None}])
    out = tools["get_order"](order_id="999")
    assert out == "Order 999 not found."


def test_get_order_formats_single_order_with_line_items_and_unit_prices():
    tools, fc = _build([{"order": _order_node(
        oid="1001", total="85.00",
        line_items=[
            _line_item("Tee", 2, unit_price="25.00"),
            _line_item("Hat", 1, unit_price="35.00"),
        ],
        referring_site="https://tiktok.com/@gss",
        display_financial_status="PAID",
        display_fulfillment_status="FULFILLED",
    )}])
    out = tools["get_order"](order_id="1001")
    assert "Order: #1001 (id: 1001)" in out
    assert "Total: $85.00" in out
    assert "Status: PAID / FULFILLED" in out
    assert "Traffic source: https://tiktok.com/@gss" in out
    assert "Tee x2 — $25.00" in out
    assert "Hat x1 — $35.00" in out


def test_get_order_traffic_source_falls_back_to_direct():
    tools, fc = _build([{"order": _order_node(referring_site=None)}])
    out = tools["get_order"](order_id="1001")
    assert "Traffic source: direct" in out


def test_get_order_handles_null_financial_and_fulfillment_status():
    """Shopify can return null for either status (partially fulfilled, etc.).
    Render the null rather than crash."""
    tools, fc = _build([{"order": _order_node(
        oid="1001",
        display_financial_status=None,
        display_fulfillment_status=None,
    )}])
    out = tools["get_order"](order_id="1001")
    assert "Status: None / None" in out


def test_get_order_handles_missing_unit_price_gracefully():
    tools, fc = _build([{"order": _order_node(
        line_items=[{"name": "Freebie", "quantity": 1}],  # no originalUnitPriceSet
    )}])
    out = tools["get_order"](order_id="1001")
    assert "Freebie x1 — $N/A" in out


def test_get_order_gid_plumbing_normalizes_numeric_id():
    tools, fc = _build([{"order": _order_node(oid="1001")}])
    tools["get_order"](order_id="1001")
    assert fc.calls[0][0] == GET_ORDER_BY_ID
    assert fc.calls[0][1] == {"id": "gid://shopify/Order/1001"}
