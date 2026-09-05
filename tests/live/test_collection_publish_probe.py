"""
Live verification for the collection publication tools — Story 10.83, card step 8.

The offline suite proves the tools send what we think they send against a
scripted FakeClient. Only this runner answers the questions that decide whether
the feature actually works:

  1. Does `confirm=False` really leave the store untouched?
  2. Does a confirmed publish really put the collection on Online Store, with a
     publish date readable back through our own read tool?
  3. **Does the storefront collection page actually render afterwards?** This is
     the card's real acceptance test and no GraphQL assertion substitutes for
     it — a collection can be "published" and still 404 if publishing is not
     what makes the page reachable.
  4. Does unpublishing reverse all of it?

WRITES TO A LIVE STORE, and briefly makes a collection publicly reachable.
`PROBE_HANDLE` must name a collection that is **already unpublished on every
channel** and that nobody minds being visible for the duration of one test run.
The runner restores the original state at the end of the sequence, and the
final test asserts the restore actually happened rather than assuming it.

This runner does NOT create a collection — Story 10.83's scope guard forbids it.
It operates on a collection that already exists.

Not run by CI (tests/conftest.py holds tests/live back from collection); it
needs SHOPIFY_STORE_URL + SHOPIFY_ACCESS_TOKEN. Run it deliberately, in order:

  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/live/test_collection_publish_probe.py -v -s
"""

import time
import uuid

import pytest
import requests

from shopify_mcp.client import ShopifyClient
from shopify_mcp.shopify.operations import publications as ops
from shopify_mcp.tools import publications as tools_publications
from tests.support import CapturingServer

# A collection that already exists and is on NO sales channel. Change this
# before running against a different store.
PROBE_HANDLE = "zz-probe-story-10-82-1788594230"
CHANNEL = "Online Store"


@pytest.fixture(scope="module")
def client():
    return ShopifyClient()


@pytest.fixture(scope="module")
def tools(client):
    srv = CapturingServer()
    tools_publications.register(srv, client)
    return srv.tools


def _published_channel_names(client) -> set[str]:
    """Read the collection's channels straight from the API, not through our
    own tool — so the verification does not depend on the code under test."""
    _col, rps, _capped = ops.read_collection_publications(client, PROBE_HANDLE)
    return {(rp.get("publication") or {}).get("name") for rp in rps if rp.get("isPublished")}


def _storefront_status(client) -> int:
    """Fetch the storefront collection page, defeating both things that make a
    naive check lie.

    Redirects are followed: the .myshopify.com host 301s to the store's primary
    domain, so `allow_redirects=False` reports 301 whether the collection is
    published or not and proves nothing either way.

    A unique query parameter is appended on every call so the edge cache is not
    a variable. It is NOT the whole story and does not on its own make the
    reversal check reliable: on 2026-09-05 a cache-busted fetch still returned
    200 for roughly 30 seconds after the unpublish landed. That residue is
    Shopify propagation, not Cloudflare — see `_await_storefront_status`, which
    is what actually handles it. Do not remove the polling on the strength of
    this cache-buster."""
    url = (
        f"https://{client._settings.shopify_store_url}"
        f"/collections/{PROBE_HANDLE}?cb={uuid.uuid4().hex}"
    )
    resp = requests.get(url, timeout=30, allow_redirects=True)
    hops = " -> ".join(str(r.status_code) for r in resp.history)
    print(f"\n[probe] GET {url} -> [{hops}] final {resp.status_code} at {resp.url}\n")
    return resp.status_code


def _await_storefront_status(client, expected: int, timeout_s: int = 120) -> int:
    """Poll the storefront until it reports `expected`, or give up.

    The storefront is **eventually consistent** with the Admin API. On
    2026-09-05 a `publishableUnpublish` that the Admin API reported as complete
    — `resourcePublications` already showed no channels — still served 200 at
    the storefront for tens of seconds, cache-busted, before turning 404.
    Asserting a single immediate fetch reads that lag as a failure.

    This is the behaviour that matters to anyone scripting publish-then-check:
    the Admin API going quiet does not mean the page has flipped yet."""
    deadline = time.monotonic() + timeout_s
    status = _storefront_status(client)
    while status != expected and time.monotonic() < deadline:
        time.sleep(5)
        status = _storefront_status(client)
    return status


@pytest.fixture(scope="module", autouse=True)
def _require_an_unpublished_probe(client):
    """Hard precondition, enforced with pytest.exit rather than an assertion.

    A failed test does NOT stop the run — pytest carries on to the next one. So
    a plain `assert` here would report the problem and then let the publish and
    unpublish legs execute anyway, leaving the live store CHANGED from how it
    was found while the final restore check still passed against a hardcoded
    empty set. For a runner that mutates a real store the guard has to actually
    halt, and the restore has to be measured against what was really there."""
    before = _published_channel_names(client)
    if before:
        pytest.exit(
            f"PROBE_HANDLE {PROBE_HANDLE!r} is already published to {sorted(before)}; "
            "this runner only operates on a collection that is on no channel. "
            "Pick another handle rather than mutating a live listing.",
            returncode=1,
        )
    return before


def test_preview_changes_nothing_on_the_store(tools, client):
    out = tools["publish_collection_to_channels"](
        handle=PROBE_HANDLE, channel_names=[CHANNEL], confirm=False
    )
    print(f"\n[probe] preview:\n{out}\n")
    assert "PREVIEW — Publish collection to channels" in out
    assert "confirm=True" in out
    assert _published_channel_names(client) == set()


def test_confirmed_publish_puts_the_collection_on_online_store(tools, client):
    out = tools["publish_collection_to_channels"](
        handle=PROBE_HANDLE, channel_names=[CHANNEL], confirm=True
    )
    print(f"\n[probe] publish:\n{out}\n")
    assert out.startswith("CONFIRMED — Publish collection to channels")
    assert CHANNEL in _published_channel_names(client)


def test_our_own_read_tool_reports_the_publish_date(tools):
    out = tools["get_collection_publications"](handle=PROBE_HANDLE)
    print(f"\n[probe] read back:\n{out}\n")
    published_section = out[out.index("Published to") : out.index("Not published to")]
    assert CHANNEL in published_section
    assert "publishDate" in published_section


def test_the_storefront_collection_page_renders(client):
    """The card's actual acceptance test. A 200 here is what "the collection is
    reachable" means; every GraphQL assertion above is a proxy for it."""
    assert _await_storefront_status(client, 200) == 200


def test_confirmed_unpublish_removes_it_again(tools, client):
    out = tools["unpublish_collection_from_channels"](
        handle=PROBE_HANDLE, channel_names=[CHANNEL], confirm=True
    )
    print(f"\n[probe] unpublish:\n{out}\n")
    assert out.startswith("CONFIRMED — Unpublish collection from channels")
    assert CHANNEL not in _published_channel_names(client)


def test_the_storefront_page_is_gone_again(client):
    assert _await_storefront_status(client, 404) == 404


def test_the_store_was_left_as_it_was_found(client, _require_an_unpublished_probe):
    """Asserts the restore against the state actually captured before the run,
    not against a hardcoded empty set."""
    assert _published_channel_names(client) == _require_an_unpublished_probe
