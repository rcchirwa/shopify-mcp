"""
Webhook tool test — validates list_webhooks / register_webhook / delete_webhook
end-to-end against the live Shopify store.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  python3 test_webhooks.py

Guarantees cleanup: any webhook created by this test is deleted before exit,
even on failure partway through.
"""

import re
import sys

import tools.webhooks as webhooks_module
from shopify_client import ShopifyClient

TEST_TOPIC = "ORDERS_CREATE"
TEST_ENDPOINT = "https://httpbin.org/post"


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


def _extract_subscription_id(output: str) -> str:
    m = re.search(r"Subscription ID\s*:\s*(\d+)", output)
    if not m:
        _fail("register_webhook(confirm=True)", f"no subscription id in output:\n{output}")
    return m.group(1)


def main():
    client = ShopifyClient()
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
    preview = register_webhook(topic=TEST_TOPIC, endpoint_url=TEST_ENDPOINT)
    if "PREVIEW" not in preview or TEST_TOPIC not in preview or TEST_ENDPOINT not in preview:
        _fail("Step 2", f"preview missing expected fields:\n{preview}")
    print("Step 2 PASSED.\n")

    sub_id = None
    needs_cleanup = False
    try:
        print("Step 3 — register_webhook confirm=True")
        created = register_webhook(topic=TEST_TOPIC, endpoint_url=TEST_ENDPOINT, confirm=True)
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
