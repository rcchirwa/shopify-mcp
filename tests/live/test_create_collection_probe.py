"""
Live probe for create_collection — the questions the offline suite cannot answer.

Story 10.82 (T-collection-create), card implementation step 7. `FakeClient`
returns canned dicts, so every offline test here proves only that our code
sends what we think it sends. Three things are decided by Shopify, not by us:

  1. Does a preview really leave the handle absent on the store?
  2. Does a confirmed create really produce a MANUAL collection (no ruleSet)
     with the sanitized description, unpublished on every sales channel?
  3. What does Shopify do with an EXPLICITLY-supplied handle that is already
     taken — a `userErrors` entry, or the silent `-1` suffix it applies to
     auto-derived handles? This is not knowable from source, and the answer is
     recorded verbatim in docs/tech-debt.md.

WRITES TO A LIVE STORE. This is the only runner in tests/live that mutates.
It creates up to two collections and there is NO delete tool in this server —
clean up by hand in the Shopify admin afterwards. `PROBE_HANDLE` is namespaced
and timestamped so a run can never collide with real merchandising.

Not run by CI (tests/conftest.py holds tests/live back from collection); it
needs SHOPIFY_STORE_URL + SHOPIFY_ACCESS_TOKEN. Run it deliberately, once:

  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/live/test_create_collection_probe.py -v -s
"""

import time

import pytest

from shopify_mcp.client import ShopifyClient
from shopify_mcp.shopify.operations import collections as ops
from shopify_mcp.tools import collections as tools_collections
from tests.support import CapturingServer

PROBE_TITLE = "ZZ Probe Story 10.82"
PROBE_HANDLE = f"zz-probe-story-10-82-{int(time.time())}"
PROBE_DESCRIPTION = '<p>probe</p><iframe src="https://evil.example"></iframe>'


@pytest.fixture(scope="module")
def client():
    return ShopifyClient()


@pytest.fixture(scope="module")
def tools(client):
    srv = CapturingServer()
    tools_collections.register(srv, client)
    return srv.tools


def test_preview_leaves_the_handle_absent_on_the_store(tools, client):
    """Question 1 — AC2's live half. The preview must not create anything."""
    out = tools["create_collection"](
        title=PROBE_TITLE, handle=PROBE_HANDLE, description=PROBE_DESCRIPTION
    )
    assert "PREVIEW — Collection create" in out
    assert ops.read_collection_by_handle(client, PROBE_HANDLE) is None


def test_confirmed_create_produces_a_manual_unpublished_collection(tools, client):
    """Question 2 — AC1. Reads the collection back rather than trusting the
    mutation's own echo."""
    out = tools["create_collection"](
        title=PROBE_TITLE,
        handle=PROBE_HANDLE,
        description=PROBE_DESCRIPTION,
        confirm=True,
    )
    print(f"\n[probe] create result:\n{out}\n")
    assert out.startswith("Done.")

    col = ops.read_collection_by_handle(client, PROBE_HANDLE)
    assert col is not None, "collection was not created"
    assert col["title"] == PROBE_TITLE
    assert col["handle"] == PROBE_HANDLE
    # Manual, per decision 1: a smart collection would carry a ruleSet.
    assert not col.get("ruleSet")
    # The sanitizer ran before the write, not just in the preview.
    assert "<iframe" not in (col.get("descriptionHtml") or "")
    assert "<p>probe</p>" in (col.get("descriptionHtml") or "")


def test_created_collection_is_on_no_sales_channel(client):
    """Question 2, publishing half — the scope guard's live check."""
    data = client.execute(
        """
        query ProbePublications($handle: String!) {
          collectionByHandle(handle: $handle) {
            resourcePublicationsCount { count }
          }
        }
        """,
        {"handle": PROBE_HANDLE},
    )
    count = data["collectionByHandle"]["resourcePublicationsCount"]["count"]
    print(f"\n[probe] resourcePublicationsCount = {count}\n")
    assert count == 0, f"expected an unpublished collection, got {count} publications"


def test_what_shopify_does_with_an_explicitly_taken_handle(client):
    """Question 3 — the one the card says may overturn decision 3.

    Goes STRAIGHT TO THE OPERATION, bypassing the tool: the tool's pre-read
    would refuse first, which is exactly the behaviour under test elsewhere.
    Here we need Shopify's own answer, so the guard is stepped around.

    This test asserts only that Shopify answered in one of the two shapes the
    card anticipated. Whichever it was gets printed and copied verbatim into
    docs/tech-debt.md. A third shape means going back to card step 3.
    """
    result = ops.create_collection(client, title=PROBE_TITLE, handle=PROBE_HANDLE)
    payload = result["collectionCreate"]
    print(f"\n[probe] SECOND create on a taken handle returned:\n{payload}\n")

    user_errors = payload.get("userErrors") or []
    collection = payload.get("collection")

    if user_errors:
        # Shape A: refused. Nothing to clean up.
        assert collection is None
    else:
        # Shape B: silently suffixed. A SECOND collection now exists and must
        # be deleted by hand in the admin — this server has no delete tool.
        assert collection is not None
        assert collection["handle"] != PROBE_HANDLE, (
            "Shopify returned no userErrors AND the requested handle — "
            "neither anticipated shape; revisit card step 3"
        )
        print(f"\n[probe] MANUAL CLEANUP REQUIRED: {collection['handle']}\n")
