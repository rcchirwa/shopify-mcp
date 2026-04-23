"""
Offline unit tests for tools/webhooks.py.

Uses a scripted FakeClient to exercise list formatting, the preview/confirm
gate, userErrors handling, and GID normalization. Live GraphQL (test_webhooks.py)
is intentionally excluded from CI because it needs real Shopify credentials.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_webhooks_offline.py -v
"""

import pytest

from _testing import CapturingServer, FakeClient
from tools import webhooks
from tools.webhooks import CREATE_WEBHOOK, DELETE_WEBHOOK, LIST_WEBHOOKS


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    monkeypatch.setattr(webhooks, "log_write", lambda *a, **k: None)


def _build(responses):
    srv = CapturingServer()
    fc = FakeClient(responses)
    webhooks.register(srv, fc)
    return srv.tools, fc


_DEFAULT_ENDPOINT = {
    "__typename": "WebhookHttpEndpoint",
    "callbackUrl": "https://example.com/hook",
}


def _node(sub_id="42", topic="ORDERS_CREATE", endpoint=_DEFAULT_ENDPOINT):
    return {
        "id": f"gid://shopify/WebhookSubscription/{sub_id}",
        "topic": topic,
        "format": "JSON",
        "createdAt": "2026-04-20T10:00:00Z",
        "apiVersion": {"handle": "2025-10"},
        "endpoint": endpoint,
    }


# ---------- list_webhooks ----------


def test_list_webhooks_empty():
    tools, fc = _build([{"webhookSubscriptions": {"nodes": []}}])
    out = tools["list_webhooks"]()
    assert out == "No webhooks registered."
    assert fc.calls[0][0] == LIST_WEBHOOKS


def test_list_webhooks_formats_http_node():
    tools, fc = _build([{"webhookSubscriptions": {"nodes": [_node()]}}])
    out = tools["list_webhooks"]()
    assert "Webhook subscriptions (1):" in out
    assert "[42]" in out  # numeric id via from_gid
    assert "ORDERS_CREATE" in out
    assert "https://example.com/hook" in out
    assert "format=JSON" in out
    assert "api=2025-10" in out
    assert "created=2026-04-20" in out  # date prefix only


def test_list_webhooks_non_http_endpoint_falls_back_to_typename():
    node = _node(endpoint={"__typename": "WebhookEventBridgeEndpoint"})
    tools, fc = _build([{"webhookSubscriptions": {"nodes": [node]}}])
    out = tools["list_webhooks"]()
    assert "(WebhookEventBridgeEndpoint)" in out


def test_list_webhooks_missing_endpoint():
    node = _node(endpoint=None)
    tools, fc = _build([{"webhookSubscriptions": {"nodes": [node]}}])
    out = tools["list_webhooks"]()
    assert "(no endpoint)" in out


def test_list_webhooks_clamps_limit_to_250():
    tools, fc = _build([{"webhookSubscriptions": {"nodes": []}}])
    tools["list_webhooks"](limit=500)
    assert fc.calls[0][1] == {"first": 250}


# ---------- register_webhook ----------


def test_register_preview_does_not_mutate():
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://example.com/hook",
    )
    assert out.startswith("PREVIEW — Register webhook")
    assert "ORDERS_CREATE" in out
    assert "https://example.com/hook" in out
    assert "JSON" in out
    assert "confirm=True" in out
    assert len(fc.calls) == 0


def test_register_confirmed_submits_create():
    tools, fc = _build(
        [
            {
                "webhookSubscriptionCreate": {
                    "webhookSubscription": {
                        "id": "gid://shopify/WebhookSubscription/42",
                        "topic": "ORDERS_CREATE",
                        "format": "JSON",
                        "endpoint": {
                            "__typename": "WebhookHttpEndpoint",
                            "callbackUrl": "https://example.com/hook",
                        },
                    },
                    "userErrors": [],
                }
            }
        ]
    )
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://example.com/hook",
        confirm=True,
    )
    assert out.startswith("Done.")
    assert "42" in out  # numeric id surfaced via from_gid
    query, variables = fc.calls[0]
    assert query == CREATE_WEBHOOK
    assert variables == {
        "topic": "ORDERS_CREATE",
        "webhookSubscription": {
            "callbackUrl": "https://example.com/hook",
            "format": "JSON",
        },
    }


def test_register_confirmed_surfaces_user_errors():
    tools, fc = _build(
        [
            {
                "webhookSubscriptionCreate": {
                    "webhookSubscription": None,
                    "userErrors": [
                        {
                            "field": ["webhookSubscription", "callbackUrl"],
                            "message": "must be https",
                        },
                    ],
                }
            }
        ]
    )
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="http://insecure.example.com/hook",
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "must be https" in out


def test_register_forwards_xml_format():
    tools, fc = _build(
        [
            {
                "webhookSubscriptionCreate": {
                    "webhookSubscription": {
                        "id": "gid://shopify/WebhookSubscription/99",
                        "topic": "ORDERS_CREATE",
                        "format": "XML",
                        "endpoint": {
                            "__typename": "WebhookHttpEndpoint",
                            "callbackUrl": "https://example.com/hook",
                        },
                    },
                    "userErrors": [],
                }
            }
        ]
    )
    tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://example.com/hook",
        message_format="XML",
        confirm=True,
    )
    _, variables = fc.calls[0]
    assert variables["webhookSubscription"]["format"] == "XML"


# ---------- delete_webhook ----------


def test_delete_preview_numeric_id_no_mutation():
    tools, fc = _build([])
    out = tools["delete_webhook"](subscription_id="123")
    assert out.startswith("PREVIEW — Delete webhook")
    assert "Subscription ID : 123" in out
    assert "confirm=True" in out
    assert len(fc.calls) == 0


def test_delete_preview_accepts_full_gid():
    tools, fc = _build([])
    out = tools["delete_webhook"](
        subscription_id="gid://shopify/WebhookSubscription/123",
    )
    assert "Subscription ID : 123" in out  # from_gid normalized
    assert len(fc.calls) == 0


def test_delete_confirmed_submits_mutation_with_gid():
    tools, fc = _build(
        [
            {
                "webhookSubscriptionDelete": {
                    "deletedWebhookSubscriptionId": "gid://shopify/WebhookSubscription/123",
                    "userErrors": [],
                }
            }
        ]
    )
    out = tools["delete_webhook"](subscription_id="123", confirm=True)
    assert out.startswith("Done.")
    query, variables = fc.calls[0]
    assert query == DELETE_WEBHOOK
    assert variables == {"id": "gid://shopify/WebhookSubscription/123"}


def test_delete_confirmed_with_full_gid_input_sends_canonical_gid():
    tools, fc = _build(
        [
            {
                "webhookSubscriptionDelete": {
                    "deletedWebhookSubscriptionId": "gid://shopify/WebhookSubscription/123",
                    "userErrors": [],
                }
            }
        ]
    )
    tools["delete_webhook"](
        subscription_id="gid://shopify/WebhookSubscription/123",
        confirm=True,
    )
    _, variables = fc.calls[0]
    # No double-prefix — from_gid strips, to_gid rebuilds cleanly.
    assert variables == {"id": "gid://shopify/WebhookSubscription/123"}


def test_delete_confirmed_surfaces_user_errors():
    tools, fc = _build(
        [
            {
                "webhookSubscriptionDelete": {
                    "deletedWebhookSubscriptionId": None,
                    "userErrors": [{"field": ["id"], "message": "not found"}],
                }
            }
        ]
    )
    out = tools["delete_webhook"](subscription_id="123", confirm=True)
    assert out.startswith("Error:")
    assert "not found" in out


def test_delete_confirmed_missing_id_and_no_errors_is_error():
    tools, fc = _build(
        [
            {
                "webhookSubscriptionDelete": {
                    "deletedWebhookSubscriptionId": None,
                    "userErrors": [],
                }
            }
        ]
    )
    out = tools["delete_webhook"](subscription_id="123", confirm=True)
    assert "Error" in out
    assert "deletedWebhookSubscriptionId" in out
