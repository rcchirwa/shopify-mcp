"""
Webhook tool test — validates list_webhooks / register_webhook / delete_webhook
end-to-end against the live Shopify store.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  export SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST=1
  python3 tests/live/test_webhooks.py

Cleanup contract: a subscription whose id the runner knows is deleted before
exit, even on failure partway through. When the runner cannot learn the id after
a create that may have landed (the confirmed reply has no id, or the call
raised), it prints MANUAL CLEANUP REQUIRED with the topic and the endpoint and
deletes nothing, because it cannot tell that subscription from any other one on
the store. A create that Shopify refused (a reply that shows nothing was
created) needs no cleanup: the runner fails Step 3 and prints no alarm.

Guarded runner (SEC-16 / Story 10.49). This test registers a real webhook
subscription on the configured store, so three checks run before anything is
registered:

  1. ``SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST=1`` must be set explicitly, or the
     runner exits before touching the API at all.
  2. The registration endpoint is read from ``WEBHOOK_RECEIVER_URL`` — a host
     the project controls, not a third party — never a literal in this file.
  3. The configured store must report itself as a development store
     (``shop.plan.partnerDevelopment``), or the runner refuses to register
     against it rather than risk a live registration against production.

Whatever host ``WEBHOOK_RECEIVER_URL`` names, it is never a third party — but
``register_webhook`` itself (``tools/webhooks.py::_check_endpoint``, SEC-17 /
Story 10.51) additionally requires that host to be listed in
``WEBHOOK_ALLOWLIST_HOSTS`` before it will register, *unless* the operator has
set the ``WEBHOOK_ALLOW_ANY_HOST=true`` escape hatch — in which case that
extra check is bypassed and any https host is accepted.

The test topic is ``PRODUCTS_UPDATE`` rather than ``ORDERS_CREATE``: it
exercises the identical register/list/delete round trip without the delivered
payload ever carrying customer PII, shrinking what the registration window
(step 3 to cleanup) can expose even though it can't be closed entirely.
"""

import os
import re
import sys
from collections.abc import Mapping

import shopify_mcp.tools.webhooks as webhooks_module
from shopify_mcp.client import ShopifyClient
from shopify_mcp.tools._untrusted import INJECTION_REMINDER

ALLOW_ENV_VAR = "SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST"
ENDPOINT_ENV_VAR = "WEBHOOK_RECEIVER_URL"

TEST_TOPIC = "PRODUCTS_UPDATE"

MANUAL_CLEANUP_MARKER = "MANUAL CLEANUP REQUIRED"

SHOP_PLAN_QUERY = """
query {
  shop {
    myshopifyDomain
    plan { partnerDevelopment }
  }
}
"""


class _Capture:
    """Minimal stand-in for FastMCP that records tools registered via
    @server.tool() so the test can invoke them as plain callables.

    Accepts the `annotations=` keyword webhooks.py passes (Story 10.73) and
    ignores it; would need updating if webhooks.py ever starts using
    @server.tool(name=..., description=...) or similar.
    """

    def __init__(self):
        self.tools = {}

    def tool(self, *, annotations=None):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _fail(step: str, detail: str):
    print(f"{step} FAILED: {detail}")
    sys.exit(1)


def require_opt_in(env: Mapping[str, str] | None = None) -> None:
    """Abort before any API call unless the opt-in var is set to '1'."""
    env = os.environ if env is None else env
    if env.get(ALLOW_ENV_VAR) != "1":
        _fail(
            "Startup guard",
            f"{ALLOW_ENV_VAR} is not set to '1'. This runner registers a real "
            f"webhook subscription on the configured store — set "
            f"{ALLOW_ENV_VAR}=1 to run it deliberately.",
        )


def require_endpoint(env: Mapping[str, str] | None = None) -> str:
    """Read the registration endpoint from WEBHOOK_RECEIVER_URL — a host the
    project controls — rather than hardcoding any endpoint in source."""
    env = os.environ if env is None else env
    endpoint = (env.get(ENDPOINT_ENV_VAR) or "").strip()
    if not endpoint:
        _fail(
            "Startup guard",
            f"{ENDPOINT_ENV_VAR} is not set. This runner registers its test "
            f"webhook against a project-controlled endpoint, not a third "
            f"party — set {ENDPOINT_ENV_VAR} to a host already listed in "
            f"WEBHOOK_ALLOWLIST_HOSTS.",
        )
    return endpoint


def require_development_store(client) -> None:
    """Abort before registering unless the store reports itself as a
    development store, so a misconfigured SHOPIFY_STORE_URL can't run this
    against production. Runs after the cheap local checks (opt-in, endpoint)
    since it costs a real network round trip."""
    try:
        data = client.execute(SHOP_PLAN_QUERY)
    except Exception as e:
        _fail("Startup guard", f"could not read shop.plan to verify a development store: {e}")
    shop = data.get("shop") or {}
    plan = shop.get("plan") or {}
    if plan.get("partnerDevelopment") is not True:
        _fail(
            "Startup guard",
            f"SHOPIFY_STORE_URL ({shop.get('myshopifyDomain', '?')}) is not a "
            f"development store (shop.plan.partnerDevelopment is not true) — "
            f"refusing to register a live webhook against it.",
        )


def _extract_subscription_id(output: str) -> str:
    m = re.search(r"Subscription ID\s*:\s*(\d+)", output)
    if not m:
        _fail("register_webhook(confirm=True)", f"no subscription id in output:\n{output}")
    return m.group(1)


def _is_refused_create(reply: object) -> bool:
    """True if ``reply`` shows that Shopify created nothing.

    Review round 1 of Story 10.96 (Robert amended D12). Exactly two reply shapes
    count, both from ``register_webhook(confirm=True)``:

    (a) the reply starts with ``Error:``: a pre-create refusal from
        ``tools/webhooks.py::_check_endpoint`` (scheme, allowlist, allowlist not
        configured);
    (b) the reply starts with ``INJECTION_REMINDER`` and the text after it starts
        with ``Error:``: the userErrors reply of ``write_gate``
        (``with_reminder(err)``).

    Any other reply, for example ``Subscription ID : (unknown)``, may follow a
    create that landed, so it is not a refusal. ``Error:`` later in the text does
    not count.
    """
    if not isinstance(reply, str):
        return False
    return reply.startswith("Error:") or (
        reply.startswith(INJECTION_REMINDER)
        and reply[len(INJECTION_REMINDER) :].startswith("Error:")
    )


def _print_manual_cleanup(endpoint: str) -> None:
    """Tell the operator to delete by hand a subscription that may exist.

    Story 10.96. Used when ``register_webhook(confirm=True)`` may have created
    the subscription but the runner never learned its id. Without an id the
    runner cannot tell this subscription from any other one on the store, so
    it never deletes: it names the topic and the exact endpoint instead.
    """
    print(
        f"{MANUAL_CLEANUP_MARKER}: a {TEST_TOPIC} webhook subscription pointing at "
        f"{endpoint} may exist on the store. This runner did not learn its id and "
        f"deletes nothing it cannot identify. Run list_webhooks, find the "
        f"subscription with topic {TEST_TOPIC} and endpoint {endpoint}, and delete it "
        f"with delete_webhook if it is there."
    )


def run_round_trip(list_webhooks, register_webhook, delete_webhook, test_endpoint):
    """Steps 1-6 and the cleanup ``finally`` block, with the three webhook tools
    and the endpoint passed in so offline tests can drive them with fakes.
    """
    print("Step 1 — list_webhooks (baseline)")
    baseline = list_webhooks()
    if not isinstance(baseline, str):
        _fail("Step 1", f"expected str, got {type(baseline).__name__}")
    print(f"  {baseline.splitlines()[0]}")
    print("Step 1 PASSED.\n")

    print("Step 2 — register_webhook preview")
    preview = register_webhook(topic=TEST_TOPIC, endpoint_url=test_endpoint)
    if "PREVIEW" not in preview or TEST_TOPIC not in preview or test_endpoint not in preview:
        _fail("Step 2", f"preview missing expected fields:\n{preview}")
    print("Step 2 PASSED.\n")

    sub_id = None
    needs_cleanup = False
    # Story 10.96: "created, id unknown". Armed BEFORE register_webhook(confirm=True)
    # because that call can create the subscription and then raise, or return
    # output with no parseable id. Either way no id reaches `sub_id`, so cleanup
    # by id cannot run; the `finally` block prints a manual-cleanup instruction
    # instead. Disarmed as soon as an id is parsed.
    created_id_unknown = False
    try:
        print("Step 3 — register_webhook confirm=True")
        created_id_unknown = True
        try:
            created = register_webhook(topic=TEST_TOPIC, endpoint_url=test_endpoint, confirm=True)
        except Exception as e:
            _fail("Step 3", f"register_webhook(confirm=True) raised {type(e).__name__}: {e}")
        if _is_refused_create(created):
            # Shopify created nothing, so there is nothing to clean up: disarm the
            # manual-cleanup alarm, then fail Step 3. No delete runs.
            created_id_unknown = False
            _fail(
                "Step 3", f"register_webhook(confirm=True) was refused; nothing created:\n{created}"
            )
        # Extract the subscription id and arm cleanup BEFORE asserting on the
        # confirmed-label shape below. If the subscription was actually
        # created, it must be cleaned up regardless of whether the label
        # assertion that follows passes — a future label regression (like the
        # "Done. PREVIEW — ..." defect Story 9.21 fixed) must never strand a
        # live webhook by exiting before this point.
        sub_id = _extract_subscription_id(created)
        created_id_unknown = False
        needs_cleanup = True
        print(f"  Created subscription id: {sub_id}")
        # Story 9.21: the confirmed label is "CONFIRMED — ...", not
        # "Done. ...". register_webhook can also prepend an external-domain
        # warning ahead of the header, so check presence/absence rather than
        # a fixed prefix.
        if "CONFIRMED —" not in created or "PREVIEW" in created:
            _fail("Step 3", f"expected 'CONFIRMED —' label, no 'PREVIEW':\n{created}")
        print("Step 3 PASSED.\n")

        print("Step 4 — list_webhooks shows new subscription")
        after_create = list_webhooks()
        if f"[{sub_id}]" not in after_create:
            _fail("Step 4", f"subscription {sub_id} not found in list:\n{after_create}")
        print("Step 4 PASSED.\n")

        print("Step 5 — delete_webhook confirm=True")
        deleted = delete_webhook(subscription_id=sub_id, confirm=True)
        # Disarm cleanup BEFORE the label assertion below, mirroring how
        # Step 3 arms cleanup before its own assertion: if the delete actually
        # succeeded (the subscription is gone) but the label check below
        # happens to fail, the `finally` block must not re-delete an
        # already-deleted subscription.
        if "CONFIRMED —" in deleted:
            needs_cleanup = False
        # Story 9.21: the confirmed label is "CONFIRMED — ...", not "Done. ...".
        if "CONFIRMED —" not in deleted or "PREVIEW" in deleted:
            _fail("Step 5", f"expected 'CONFIRMED —' label, no 'PREVIEW':\n{deleted}")
        print("Step 5 PASSED.\n")

        print("Step 6 — list_webhooks confirms subscription gone")
        after_delete = list_webhooks()
        if f"[{sub_id}]" in after_delete:
            _fail("Step 6", f"subscription {sub_id} still present after delete:\n{after_delete}")
        print("Step 6 PASSED.\n")

    finally:
        if created_id_unknown:
            _print_manual_cleanup(test_endpoint)
            # Exit non-zero only when nothing else is in flight, or when it is the
            # SystemExit that _fail raised. Any other exception (a KeyboardInterrupt,
            # a TypeError from a non-str reply) must reach the caller unchanged:
            # sys.exit here would replace it.
            in_flight = sys.exc_info()[1]
            if in_flight is None or isinstance(in_flight, SystemExit):
                sys.exit(1)
        if needs_cleanup and sub_id:
            print(f"CLEANUP — deleting leftover test webhook {sub_id}...")
            try:
                cleanup_result = delete_webhook(subscription_id=sub_id, confirm=True)
                # delete_webhook reports failure by RETURNING an "Error: ..."
                # string, not by raising — so a bare "no exception" check here
                # would print "CLEANUP done." for a delete that never
                # happened, hiding the fact that manual deletion is needed.
                if "CONFIRMED —" in cleanup_result:
                    print("CLEANUP done.")
                else:
                    print(
                        f"CLEANUP FAILED (manual deletion required for id={sub_id}): "
                        f"{cleanup_result}"
                    )
            except Exception as e:
                print(f"CLEANUP FAILED (manual deletion required for id={sub_id}): {e}")


def main():
    require_opt_in()
    test_endpoint = require_endpoint()
    client = ShopifyClient()
    require_development_store(client)

    capture = _Capture()
    webhooks_module.register(capture, client)

    run_round_trip(
        capture.tools["list_webhooks"],
        capture.tools["register_webhook"],
        capture.tools["delete_webhook"],
        test_endpoint,
    )
    print("All webhook tests passed.")


if __name__ == "__main__":
    main()
