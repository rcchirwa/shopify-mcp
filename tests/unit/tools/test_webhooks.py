"""
Offline unit tests for tools/webhooks.py.

Uses a scripted FakeClient to exercise list formatting, the preview/confirm
gate, userErrors handling, and GID normalization. Live GraphQL (test_webhooks.py)
is intentionally excluded from CI because it needs real Shopify credentials.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_webhooks.py -v
"""

import shopify_mcp.tools._write_tool as _wt
from shopify_mcp.tools import webhooks
from shopify_mcp.tools.webhooks import CREATE_WEBHOOK, DELETE_WEBHOOK, LIST_WEBHOOKS
from tests.support import CapturingServer, FakeClient


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


def test_register_preview_refused_when_allowlist_and_allow_any_host_unset(monkeypatch):
    """SEC-17: with no allowlist configured and no opt-out, register_webhook
    fails closed — it must refuse (not preview) and perform no mutation, even
    on the preview (confirm=False) path."""
    monkeypatch.delenv("WEBHOOK_ALLOWLIST_HOSTS", raising=False)
    monkeypatch.delenv("WEBHOOK_ALLOW_ANY_HOST", raising=False)
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://example.com/hook",
    )
    assert out.startswith("Error:")
    assert "WEBHOOK_ALLOWLIST_HOSTS" in out
    assert "WEBHOOK_ALLOW_ANY_HOST" in out
    assert len(fc.calls) == 0


def test_register_confirmed_refused_when_allowlist_and_allow_any_host_unset(monkeypatch):
    """SEC-17: confirm=True must not bypass the fail-closed default — no
    mutation call is made to the fake client."""
    monkeypatch.delenv("WEBHOOK_ALLOWLIST_HOSTS", raising=False)
    monkeypatch.delenv("WEBHOOK_ALLOW_ANY_HOST", raising=False)
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://example.com/hook",
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "WEBHOOK_ALLOWLIST_HOSTS" in out
    assert "WEBHOOK_ALLOW_ANY_HOST" in out
    assert len(fc.calls) == 0


def test_register_confirmed_submits_create(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "example.com")
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


def test_register_confirmed_surfaces_user_errors(monkeypatch):
    # Note: endpoint_url is https and allowlisted so it clears _check_endpoint
    # and reaches the mutation — this test is about surfacing Shopify-side
    # userErrors, not our own scheme/allowlist gate (covered separately below).
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "example.com")
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
        endpoint_url="https://example.com/hook",
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "must be https" in out


def test_register_forwards_xml_format(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "example.com")
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


# ---------- register_webhook — endpoint allowlist (M3 / SEC-17) ----------


def test_register_preview_allow_any_host_true_restores_external_domain_warning(monkeypatch):
    """SEC-17: WEBHOOK_ALLOW_ANY_HOST=true is the explicit opt-out that
    restores the pre-hardening warn-and-proceed behaviour byte-for-byte."""
    monkeypatch.delenv("WEBHOOK_ALLOWLIST_HOSTS", raising=False)
    monkeypatch.setenv("WEBHOOK_ALLOW_ANY_HOST", "true")
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://example.com/hook",
    )
    assert "⚠ EXTERNAL DOMAIN" in out
    assert "ORDERS_CREATE" in out
    assert len(fc.calls) == 0


def test_register_confirmed_https_with_empty_hostname_blocked(monkeypatch):
    """An https:// URL with no hostname (e.g. "https:///hook") can't
    IDNA-normalize to anything meaningful — it must be treated as "not
    matched" rather than crashing or slipping through."""
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "example.com")
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https:///hook",
        confirm=True,
    )
    assert out.startswith("Error:")
    assert len(fc.calls) == 0


def test_register_non_https_scheme_blocked_before_hostname_check(monkeypatch):
    """A non-https scheme is refused before any hostname/allowlist/network
    work — even for a host that IS allowlisted."""
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "example.com")
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="http://example.com/hook",
    )
    assert out.startswith("Error:")
    assert "https" in out.lower()
    assert len(fc.calls) == 0


def test_register_confirmed_non_https_scheme_blocked_with_allow_any_host(monkeypatch):
    """Even the WEBHOOK_ALLOW_ANY_HOST opt-out does not waive the https-only
    scheme check."""
    monkeypatch.delenv("WEBHOOK_ALLOWLIST_HOSTS", raising=False)
    monkeypatch.setenv("WEBHOOK_ALLOW_ANY_HOST", "true")
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="ftp://example.com/hook",
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "https" in out.lower()
    assert len(fc.calls) == 0


def test_register_confirmed_idna_equivalent_hostname_matches_allowlist(monkeypatch):
    """A Unicode allowlist entry and a punycode request hostname (or vice
    versa) are the same host after IDNA normalization, so it's allowed."""
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "münchen.de")
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
                            "callbackUrl": "https://xn--mnchen-3ya.de/hook",
                        },
                    },
                    "userErrors": [],
                }
            }
        ]
    )
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://xn--mnchen-3ya.de/hook",
        confirm=True,
    )
    assert out.startswith("Done.")
    assert len(fc.calls) == 1


def test_register_idna_lookalike_hostname_not_literally_allowlisted_blocked(monkeypatch):
    """A visually similar (homograph) hostname that IDNA-normalizes to a
    *different* punycode string than the allowlisted entry is not an
    IDNA-equivalent match, so it must be refused, not silently accepted."""
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "apple.com")
    tools, fc = _build([])
    # U+0430 CYRILLIC SMALL LETTER A in place of ASCII "a" (position 0) —
    # IDNA-encodes to "xn--pple-43d.com", not "apple.com".
    lookalike_host = "\u0430pple.com"
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url=f"https://{lookalike_host}/hook",
        confirm=True,
    )
    assert out.startswith("Error:")
    assert len(fc.calls) == 0


def test_register_preview_hostname_in_allowlist_no_warning(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "example.com")
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://example.com/hook",
    )
    assert "⚠" not in out
    assert "ORDERS_CREATE" in out
    assert len(fc.calls) == 0


def test_register_preview_hostname_not_in_allowlist_returns_error(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "allowed.example.com")
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://attacker.example.com/hook",
    )
    assert out.startswith("Error:")
    assert "attacker.example.com" in out
    assert "WEBHOOK_ALLOWLIST_HOSTS" in out
    assert len(fc.calls) == 0


def test_register_confirmed_hostname_in_allowlist_proceeds(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "example.com")
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
    assert len(fc.calls) == 1


def test_register_confirmed_hostname_not_in_allowlist_blocked(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "allowed.example.com")
    tools, fc = _build([])
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://attacker.example.com/hook",
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "attacker.example.com" in out
    assert len(fc.calls) == 0


# ---------- write_gate structural guarantees ----------


def test_register_log_write_not_called_on_user_error(monkeypatch):
    """write_gate ensures log_write is never called when the mutation returns userErrors."""
    monkeypatch.setenv("WEBHOOK_ALLOWLIST_HOSTS", "example.com")
    logged: list[int] = []
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: logged.append(1))

    tools, _ = _build(
        [
            {
                "webhookSubscriptionCreate": {
                    "webhookSubscription": None,
                    "userErrors": [{"field": ["callbackUrl"], "message": "invalid url"}],
                }
            }
        ]
    )
    out = tools["register_webhook"](
        topic="ORDERS_CREATE",
        endpoint_url="https://example.com/hook",
        confirm=True,
    )
    assert out.startswith("Error:")
    assert logged == []


def test_delete_log_write_not_called_on_missing_deleted_id(monkeypatch):
    """post_execute_check blocks log_write when deletedWebhookSubscriptionId absent."""
    logged: list[int] = []
    monkeypatch.setattr(_wt, "log_write", lambda *a, **k: logged.append(1))

    tools, _ = _build(
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
    assert logged == []
