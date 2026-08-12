"""
Webhook tool test — validates list_webhooks / register_webhook / delete_webhook
end-to-end against the live Shopify store.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  export SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST=1
  python3 tests/live/test_webhooks.py

Guarantees cleanup: any webhook created by this test is deleted before exit,
even on failure partway through.

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

ALLOW_ENV_VAR = "SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST"
ENDPOINT_ENV_VAR = "WEBHOOK_RECEIVER_URL"

TEST_TOPIC = "PRODUCTS_UPDATE"

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

    Assumes the no-arg form @server.tool(); would need updating if webhooks.py
    ever starts using @server.tool(name=..., description=...) or similar.
    """

    def __init__(self):
        self.tools = {}

    def tool(self):
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


def main():
    require_opt_in()
    test_endpoint = require_endpoint()
    client = ShopifyClient()
    require_development_store(client)

    capture = _Capture()
    webhooks_module.register(capture, client)

    list_webhooks = capture.tools["list_webhooks"]
    register_webhook = capture.tools["register_webhook"]
    delete_webhook = capture.tools["delete_webhook"]

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
    try:
        print("Step 3 — register_webhook confirm=True")
        created = register_webhook(topic=TEST_TOPIC, endpoint_url=test_endpoint, confirm=True)
        if not created.startswith("Done."):
            _fail("Step 3", f"expected 'Done.' prefix:\n{created}")
        sub_id = _extract_subscription_id(created)
        needs_cleanup = True
        print(f"  Created subscription id: {sub_id}")
        print("Step 3 PASSED.\n")

        print("Step 4 — list_webhooks shows new subscription")
        after_create = list_webhooks()
        if f"[{sub_id}]" not in after_create:
            _fail("Step 4", f"subscription {sub_id} not found in list:\n{after_create}")
        print("Step 4 PASSED.\n")

        print("Step 5 — delete_webhook confirm=True")
        deleted = delete_webhook(subscription_id=sub_id, confirm=True)
        if not deleted.startswith("Done."):
            _fail("Step 5", f"expected 'Done.' prefix:\n{deleted}")
        needs_cleanup = False
        print("Step 5 PASSED.\n")

        print("Step 6 — list_webhooks confirms subscription gone")
        after_delete = list_webhooks()
        if f"[{sub_id}]" in after_delete:
            _fail("Step 6", f"subscription {sub_id} still present after delete:\n{after_delete}")
        print("Step 6 PASSED.\n")

    finally:
        if needs_cleanup and sub_id:
            print(f"CLEANUP — deleting leftover test webhook {sub_id}...")
            try:
                delete_webhook(subscription_id=sub_id, confirm=True)
                print("CLEANUP done.")
            except Exception as e:
                print(f"CLEANUP FAILED (manual deletion required for id={sub_id}): {e}")

    print("All webhook tests passed.")


if __name__ == "__main__":
    main()
