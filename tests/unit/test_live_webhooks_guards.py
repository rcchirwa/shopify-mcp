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


# ---------- run_round_trip: cleanup when the subscription id is unknown ----------
#
# Story 10.96. The round-trip control flow lives in ``run_round_trip`` so these
# tests can drive it with fakes. They never call ``main()``, never build a real
# ShopifyClient and make no network call: every tool is a recording fake.

ENDPOINT = "https://receiver.example.com/webhooks"
SUB_ID = "987654321"
CONFIRMED = "CONFIRMED — webhook subscription."
MANUAL_MARKER = "MANUAL CLEANUP REQUIRED"

PREVIEW_OK = f"PREVIEW — would register {live_webhooks.TEST_TOPIC} -> {ENDPOINT}"


class _FakeWebhookTools:
    """Fake list/register/delete tools. Every call is recorded in ``calls``.

    ``register_confirmed`` is the reply to ``register_webhook(confirm=True)``
    (an Exception instance is raised instead). ``delete_replies`` is consumed
    one per ``delete_webhook`` call; running out fails the test, so a stray
    extra delete cannot pass unnoticed.
    """

    def __init__(self, register_confirmed, delete_replies=()):
        self.calls = []
        self._register_confirmed = register_confirmed
        self._delete_replies = list(delete_replies)
        self._list_replies = [
            "Webhooks (0)",
            f"Webhooks (1)\n  [{SUB_ID}] {live_webhooks.TEST_TOPIC} -> {ENDPOINT}",
            "Webhooks (0)",
        ]

    def list_webhooks(self):
        self.calls.append(("list_webhooks",))
        return self._list_replies.pop(0)

    def register_webhook(self, topic, endpoint_url, confirm=False):
        self.calls.append(("register_webhook", topic, endpoint_url, confirm))
        if not confirm:
            return PREVIEW_OK
        if isinstance(self._register_confirmed, Exception):
            raise self._register_confirmed
        return self._register_confirmed

    def delete_webhook(self, subscription_id, confirm=False):
        self.calls.append(("delete_webhook", subscription_id, confirm))
        assert self._delete_replies, "unexpected extra delete_webhook call"
        reply = self._delete_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def names(self):
        return [c[0] for c in self.calls]

    def run(self):
        live_webhooks.run_round_trip(
            self.list_webhooks, self.register_webhook, self.delete_webhook, ENDPOINT
        )


def _created(sub_id=SUB_ID):
    return f"{CONFIRMED}\n  Subscription ID : {sub_id}\n  Topic : PRODUCTS_UPDATE"


def _manual_cleanup_text(out: str) -> str:
    """Everything printed from the manual-cleanup marker on, so an assertion
    on topic or endpoint cannot pass on text printed earlier in the run."""
    assert MANUAL_MARKER in out, f"no manual-cleanup output:\n{out}"
    return out[out.index(MANUAL_MARKER) :]


def _assert_manual_cleanup_and_no_delete(tools, excinfo, out):
    assert excinfo.value.code not in (0, None)
    # Prove the path ran: the confirmed register call was made.
    assert ("register_webhook", live_webhooks.TEST_TOPIC, ENDPOINT, True) in tools.calls
    manual = _manual_cleanup_text(out)
    assert live_webhooks.TEST_TOPIC in manual
    assert ENDPOINT in manual
    assert "may exist" in manual
    assert "delete_webhook" not in tools.names()


def test_run_round_trip_id_less_output_prints_manual_cleanup_and_never_deletes(capsys):
    # The confirmed output renders "(unknown)" when the create response had no
    # id. The reply deliberately omits the endpoint so only the manual-cleanup
    # output can supply it.
    tools = _FakeWebhookTools(f"{CONFIRMED}\n  Subscription ID : (unknown)")
    with pytest.raises(SystemExit) as excinfo:
        tools.run()
    _assert_manual_cleanup_and_no_delete(tools, excinfo, capsys.readouterr().out)


def test_run_round_trip_register_raising_prints_manual_cleanup_and_never_deletes(capsys):
    tools = _FakeWebhookTools(TimeoutError("response timed out"))
    with pytest.raises(SystemExit) as excinfo:
        tools.run()
    out = capsys.readouterr().out
    assert "response timed out" in out
    _assert_manual_cleanup_and_no_delete(tools, excinfo, out)


# ---------- run_round_trip: known id keeps today's behaviour ----------


def test_run_round_trip_known_id_successful_delete_runs_one_delete(capsys):
    tools = _FakeWebhookTools(_created(), delete_replies=[CONFIRMED])
    tools.run()
    out = capsys.readouterr().out
    assert tools.names().count("delete_webhook") == 1
    assert ("delete_webhook", SUB_ID, True) in tools.calls
    assert "CLEANUP" not in out
    assert MANUAL_MARKER not in out
    assert "Step 6 PASSED." in out  # the whole round trip ran


@pytest.mark.parametrize(
    ("retry_reply", "expected"),
    [
        (CONFIRMED, "CLEANUP done."),
        ("Error: Shopify refused the delete", "CLEANUP FAILED (manual deletion required"),
    ],
)
def test_run_round_trip_known_id_failed_step_5_delete_retries_once_in_finally(
    capsys, retry_reply, expected
):
    step_5_reply = "Error: Shopify refused the delete"
    tools = _FakeWebhookTools(_created(), delete_replies=[step_5_reply, retry_reply])
    with pytest.raises(SystemExit) as excinfo:
        tools.run()
    out = capsys.readouterr().out
    assert excinfo.value.code not in (0, None)
    # Step 5 and the finally block each delete once: two calls, both by id.
    assert [c for c in tools.calls if c[0] == "delete_webhook"] == [
        ("delete_webhook", SUB_ID, True),
        ("delete_webhook", SUB_ID, True),
    ]
    assert "Step 5 FAILED" in out
    assert expected in out
    if expected != "CLEANUP done.":
        assert f"id={SUB_ID}" in out
    assert MANUAL_MARKER not in out
