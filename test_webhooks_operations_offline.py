"""
Offline unit tests for shopify.operations.webhooks.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the webhooks operations are callable from non-MCP
entry points (Story 10.31 / A5, AC4). webhooks has no by-id/by-handle read pair
(one list read + two mutations) and no entity-core selection shared across a read
pair, so no shared GraphQL fragment applies — the small ``endpoint { ... }`` union
block that recurs between the list read and the create mutation is left inline,
mirroring the un-factored ``{ shopMoney { amount } }`` money block in orders
(Story 10.31 / A5, AC3). That "no forced fragment" decision is pinned by
test_no_shared_fragment_in_webhooks_queries.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_webhooks_operations_offline.py -v
"""

from _testing import FakeClient
from shopify.operations import webhooks as ops
from shopify.queries import webhooks as q

# ---------- AC3: no shared fragment applies to webhooks ----------


def test_no_shared_fragment_in_webhooks_queries():
    """webhooks has no by-id/by-handle pair and no entity-core shared across a read
    pair, so none of its GraphQL strings declares or spreads a named fragment. The
    inline ``... on WebhookHttpEndpoint`` union selection is a type condition, not a
    named fragment, so it is intentionally not asserted against here."""
    for query in (q.LIST_WEBHOOKS, q.CREATE_WEBHOOK, q.DELETE_WEBHOOK):
        assert "fragment" not in query


# ---------- read_webhooks (build vars + execute, return node list) ----------


def _node(sub_id="42", topic="ORDERS_CREATE"):
    return {
        "id": f"gid://shopify/WebhookSubscription/{sub_id}",
        "topic": topic,
        "format": "JSON",
        "createdAt": "2026-04-20T10:00:00Z",
        "apiVersion": {"handle": "2025-10"},
        "endpoint": {
            "__typename": "WebhookHttpEndpoint",
            "callbackUrl": "https://example.com/hook",
        },
    }


def test_read_webhooks_returns_nodes_and_passes_first():
    node = _node()
    fc = FakeClient([{"webhookSubscriptions": {"nodes": [node]}}])
    out = ops.read_webhooks(fc, 50)
    assert out == [node]
    assert fc.calls[0][0] == q.LIST_WEBHOOKS
    assert fc.calls[0][1] == {"first": 50}


def test_read_webhooks_forwards_caller_first_unchanged():
    """The operation does no clamping of its own — it passes the tool's already
    clamped count straight through."""
    fc = FakeClient([{"webhookSubscriptions": {"nodes": []}}])
    ops.read_webhooks(fc, 250)
    assert fc.calls[0][1] == {"first": 250}


def test_read_webhooks_empty_returns_empty_list():
    fc = FakeClient([{"webhookSubscriptions": {"nodes": []}}])
    assert ops.read_webhooks(fc, 50) == []


def test_read_webhooks_null_nodes_returns_empty_list():
    """A permissions-trimmed / shape-drifted ``nodes: null`` yields ``[]`` rather
    than ``None`` (the ``or []`` guard the tool relied on, moved into the op)."""
    fc = FakeClient([{"webhookSubscriptions": {"nodes": None}}])
    assert ops.read_webhooks(fc, 50) == []


def test_read_webhooks_missing_connection_returns_empty_list():
    """Defensive: a response with no webhookSubscriptions connection yields an
    empty list rather than raising."""
    fc = FakeClient([{}])
    assert ops.read_webhooks(fc, 50) == []


# ---------- create_webhook (build input + execute, return raw result) ----------


def test_create_webhook_builds_input_and_returns_raw_result():
    raw = {
        "webhookSubscriptionCreate": {
            "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/42"},
            "userErrors": [],
        }
    }
    fc = FakeClient([raw])
    out = ops.create_webhook(fc, "ORDERS_CREATE", "https://example.com/hook", "JSON")
    assert out is raw
    assert fc.calls[0][0] == q.CREATE_WEBHOOK
    assert fc.calls[0][1] == {
        "topic": "ORDERS_CREATE",
        "webhookSubscription": {
            "callbackUrl": "https://example.com/hook",
            "format": "JSON",
        },
    }


def test_create_webhook_forwards_message_format():
    raw = {"webhookSubscriptionCreate": {"webhookSubscription": None, "userErrors": []}}
    fc = FakeClient([raw])
    ops.create_webhook(fc, "ORDERS_CREATE", "https://example.com/hook", "XML")
    assert fc.calls[0][1]["webhookSubscription"]["format"] == "XML"


# ---------- delete_webhook (GID coercion + execute, return raw result) ----------


def test_delete_webhook_coerces_numeric_id_to_gid():
    raw = {
        "webhookSubscriptionDelete": {
            "deletedWebhookSubscriptionId": "gid://shopify/WebhookSubscription/123",
            "userErrors": [],
        }
    }
    fc = FakeClient([raw])
    out = ops.delete_webhook(fc, "123")
    assert out is raw
    assert fc.calls[0][0] == q.DELETE_WEBHOOK
    assert fc.calls[0][1] == {"id": "gid://shopify/WebhookSubscription/123"}


def test_delete_webhook_accepts_full_gid_without_double_prefix():
    """A full GID in → the same canonical GID out (from_gid strips, to_gid rebuilds
    cleanly — no ``gid://shopify/WebhookSubscription/gid://...`` double-prefix)."""
    raw = {
        "webhookSubscriptionDelete": {
            "deletedWebhookSubscriptionId": "gid://shopify/WebhookSubscription/123",
            "userErrors": [],
        }
    }
    fc = FakeClient([raw])
    ops.delete_webhook(fc, "gid://shopify/WebhookSubscription/123")
    assert fc.calls[0][1] == {"id": "gid://shopify/WebhookSubscription/123"}
