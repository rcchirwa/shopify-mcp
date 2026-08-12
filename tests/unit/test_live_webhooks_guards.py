"""
Offline unit tests for the startup guards in tests/live/test_webhooks.py.

The live runner itself needs real Shopify credentials and is excluded from
default discovery (tests/conftest.py), so it cannot be exercised end-to-end in
CI. These guard functions are pure control flow around inputs (env vars, a
GraphQL client) that CAN be exercised offline with a scripted FakeClient —
this is the only automated proof that the SEC-16 gates (opt-in, dev-store
check, project-controlled endpoint) actually refuse before any mutation call.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/test_live_webhooks_guards.py -v
"""

import pytest

from tests.live import test_webhooks as live_webhooks
from tests.support import FakeClient

# ---------- require_opt_in ----------


def test_require_opt_in_exits_when_env_var_unset(capsys):
    with pytest.raises(SystemExit):
        live_webhooks.require_opt_in(env={})
    out = capsys.readouterr().out
    assert live_webhooks.ALLOW_ENV_VAR in out


def test_require_opt_in_exits_when_env_var_not_exactly_one(capsys):
    with pytest.raises(SystemExit):
        live_webhooks.require_opt_in(env={live_webhooks.ALLOW_ENV_VAR: "true"})
    out = capsys.readouterr().out
    assert live_webhooks.ALLOW_ENV_VAR in out


def test_require_opt_in_passes_when_var_set_to_1():
    live_webhooks.require_opt_in(env={live_webhooks.ALLOW_ENV_VAR: "1"})


# ---------- require_endpoint ----------


def test_require_endpoint_exits_when_unset(capsys):
    with pytest.raises(SystemExit):
        live_webhooks.require_endpoint(env={})
    out = capsys.readouterr().out
    assert live_webhooks.ENDPOINT_ENV_VAR in out


def test_require_endpoint_exits_when_blank(capsys):
    with pytest.raises(SystemExit):
        live_webhooks.require_endpoint(env={live_webhooks.ENDPOINT_ENV_VAR: "   "})


def test_require_endpoint_returns_configured_value():
    endpoint = live_webhooks.require_endpoint(
        env={live_webhooks.ENDPOINT_ENV_VAR: "https://receiver.example.com/webhooks"}
    )
    assert endpoint == "https://receiver.example.com/webhooks"


# ---------- require_development_store ----------


def test_require_development_store_exits_when_partner_development_false(capsys):
    fc = FakeClient(
        [
            {
                "shop": {
                    "myshopifyDomain": "prod-store.myshopify.com",
                    "plan": {"partnerDevelopment": False},
                }
            }
        ]
    )
    with pytest.raises(SystemExit):
        live_webhooks.require_development_store(fc)
    out = capsys.readouterr().out
    assert "prod-store.myshopify.com" in out


def test_require_development_store_exits_when_plan_missing(capsys):
    fc = FakeClient([{"shop": {"myshopifyDomain": "prod-store.myshopify.com"}}])
    with pytest.raises(SystemExit):
        live_webhooks.require_development_store(fc)


def test_require_development_store_passes_when_partner_development_true():
    fc = FakeClient(
        [
            {
                "shop": {
                    "myshopifyDomain": "dev-store.myshopify.com",
                    "plan": {"partnerDevelopment": True},
                }
            }
        ]
    )
    live_webhooks.require_development_store(fc)
    assert fc.calls[0][0] == live_webhooks.SHOP_PLAN_QUERY


def test_require_development_store_exits_cleanly_when_query_raises(capsys):
    """A transport/GraphQL failure must route through the same clean _fail
    path as every other guard, not propagate as a raw traceback."""
    fc = FakeClient([RuntimeError("boom")])
    with pytest.raises(SystemExit):
        live_webhooks.require_development_store(fc)
    out = capsys.readouterr().out
    assert "boom" in out


# ---------- topic swapped off the PII-bearing default ----------


def test_topic_is_not_orders_create():
    """SEC-16: ORDERS_CREATE carries customer PII; the runner must not use it."""
    assert live_webhooks.TEST_TOPIC != "ORDERS_CREATE"
