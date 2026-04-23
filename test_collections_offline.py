"""
Offline unit tests for tools/collections.py.

Covers get_collection read path plus add_product_to_collection /
remove_product_from_collection membership writes. Uses a scripted FakeClient
so no Shopify API calls or .env are required.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_collections_offline.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from tools import collections
from tools.collections import (
    GET_COLLECTION_BY_HANDLE,
    ADD_PRODUCTS_TO_COLLECTION,
    REMOVE_PRODUCTS_FROM_COLLECTION,
    UPDATE_COLLECTION,
)
from shopify_client import JOB_STATUS_QUERY
from _testing.fake_client import CapturingServer, FakeClient


class _FakeClock:
    """Drives time.monotonic forward by `interval_s` per call, so poll_job
    reaches its 10s budget deterministically without real sleep. `baseline`
    is captured at fixture setup so `monotonic()` starts at 0 and advances
    predictably regardless of actual wall-clock time."""
    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def monotonic(self):
        # Advance AFTER reading so the first call returns 0.0 (matches
        # poll_job's `start = time.monotonic()` capturing baseline).
        now = self.t
        self.t += self.step
        return now


@pytest.fixture
def fake_poll_clock(monkeypatch):
    """Skip real sleeping and fake time.monotonic inside shopify_client.poll_job
    so timeout/failure branches are fast and deterministic. Opt-in: apply by
    adding `fake_poll_clock` to a test's signature. Not autouse — future tests
    that need wall-clock timing mustn't silently pick up the fake."""
    import shopify_client
    clock = _FakeClock(step=1.0)
    monkeypatch.setattr(shopify_client.time, "sleep", lambda s: None)
    monkeypatch.setattr(shopify_client.time, "monotonic", clock.monotonic)


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    """Keep tests from polluting aon_mcp_log.txt."""
    monkeypatch.setattr(collections, "log_write", lambda *a, **k: None)


def _build(responses):
    srv = CapturingServer()
    fc = FakeClient(responses)
    collections.register(srv, fc)
    return srv.tools, fc


def _collection(handle, title, description_html=None, rule_set=None):
    return {"collectionByHandle": {
        "id": f"gid://shopify/Collection/123",
        "title": title,
        "handle": handle,
        "descriptionHtml": description_html,
        "ruleSet": rule_set,
    }}


def test_get_collection_smart_has_rule_set():
    tools, fc = _build([_collection(
        "smart-vanish", "Smart Vanish",
        description_html="<p>auto-populated</p>",
        rule_set={"appliedDisjunctively": True},
    )])
    out = tools["get_collection"](handle="smart-vanish")
    assert "Collection: Smart Vanish" in out
    assert "Handle: smart-vanish" in out
    assert "Type: smart" in out
    assert "<p>auto-populated</p>" in out
    assert fc.calls[0][0] == GET_COLLECTION_BY_HANDLE
    assert fc.calls[0][1] == {"handle": "smart-vanish"}


def test_get_collection_manual_no_rule_set():
    tools, fc = _build([_collection(
        "vanish", "Vanish Collection",
        description_html="<p>hand-curated</p>",
        rule_set=None,
    )])
    out = tools["get_collection"](handle="vanish")
    assert "Type: manual" in out
    assert "Vanish Collection" in out


def test_get_collection_not_found():
    tools, fc = _build([{"collectionByHandle": None}])
    out = tools["get_collection"](handle="vanish-clothing")
    assert out == "No collection found with handle 'vanish-clothing'."


def test_get_collection_empty_description_shown_as_placeholder():
    tools, fc = _build([_collection(
        "bare", "Bare", description_html=None, rule_set=None,
    )])
    out = tools["get_collection"](handle="bare")
    assert "Description: (no description)" in out


# ---------- membership writes ----------
#
# The response fixtures below (`_add_ok`, `_remove_ok`) reflect the
# collectionAddProductsV2 / collectionRemoveProducts payload shapes
# documented in the Shopify Admin API 2026-01 schema (shape unchanged since 2024-07):
#
#   collectionAddProductsV2: { job: Job, userErrors: [CollectionAddProductsV2UserError!]! }
#   collectionRemoveProducts: { job: Job, userErrors: [UserError!]! }
#
# Offline tests verify our code reads the right top-level keys and sends the
# right variables, but cannot catch a future Shopify shape drift. Drift is
# covered by the live-verification step documented in
# /Users/robertchirwa/.claude/plans/1-get-inventory-returns-luminous-reef.md.


def _add_ok(job_id="123", done=True):
    return {"collectionAddProductsV2": {
        "job": {"id": f"gid://shopify/Job/{job_id}", "done": done},
        "userErrors": [],
    }}


def _remove_ok(job_id="123", done=True):
    return {"collectionRemoveProducts": {
        "job": {"id": f"gid://shopify/Job/{job_id}", "done": done},
        "userErrors": [],
    }}


def _manual_collection(handle="vanish", title="Vanish"):
    return _collection(handle, title, description_html="<p>manual</p>", rule_set=None)


def _smart_collection(handle="smart-vanish", title="Smart Vanish"):
    return _collection(handle, title, description_html="<p>smart</p>",
                       rule_set={"appliedDisjunctively": True})


# --- add_product_to_collection ---

def test_add_product_preview_does_not_mutate():
    tools, fc = _build([_manual_collection()])
    out = tools["add_product_to_collection"](
        handle="vanish", product_id="777", confirm=False,
    )
    assert "PREVIEW" in out
    assert "Add product to collection" in out
    assert "Collection : Vanish" in out
    assert "Product    : 777" in out
    assert "confirm=True" in out
    # Only the resolve-collection read; no mutation call.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_COLLECTION_BY_HANDLE


def test_add_product_confirmed_calls_mutation_with_correct_gids():
    tools, fc = _build([_manual_collection(), _add_ok(job_id="999")])
    out = tools["add_product_to_collection"](
        handle="vanish", product_id="777", confirm=True,
    )
    assert out.startswith("Done.")
    assert "Added product to collection" in out
    assert "Job        : 999" in out
    mutation_query, mutation_vars = fc.calls[1]
    assert mutation_query == ADD_PRODUCTS_TO_COLLECTION
    assert mutation_vars == {
        "id": "gid://shopify/Collection/123",
        "productIds": ["gid://shopify/Product/777"],
    }


def test_add_product_rejects_smart_collection_without_mutation():
    tools, fc = _build([_smart_collection()])
    out = tools["add_product_to_collection"](
        handle="smart-vanish", product_id="777", confirm=True,
    )
    assert "Error:" in out
    assert "smart collection" in out
    assert "rule-driven" in out
    # Only the resolve-collection read; no mutation call.
    assert len(fc.calls) == 1


def test_add_product_collection_not_found():
    tools, fc = _build([{"collectionByHandle": None}])
    out = tools["add_product_to_collection"](
        handle="missing", product_id="777", confirm=True,
    )
    assert out == "No collection found with handle 'missing'."
    assert len(fc.calls) == 1


def test_add_product_rejects_empty_product_id_without_reading_collection():
    """Empty product_id should short-circuit before hitting Shopify."""
    tools, fc = _build([])
    out = tools["add_product_to_collection"](
        handle="vanish", product_id="", confirm=True,
    )
    assert out == "Provide product_id."
    assert len(fc.calls) == 0


def test_add_product_surfaces_user_errors():
    tools, fc = _build([
        _manual_collection(),
        {"collectionAddProductsV2": {
            "job": None,
            "userErrors": [
                {"field": ["productIds", "0"], "message": "Product already in collection"},
            ],
        }},
    ])
    out = tools["add_product_to_collection"](
        handle="vanish", product_id="777", confirm=True,
    )
    assert out.startswith("Error:")
    assert "Product already in collection" in out


# --- remove_product_from_collection ---

def test_remove_product_preview_does_not_mutate():
    tools, fc = _build([_manual_collection()])
    out = tools["remove_product_from_collection"](
        handle="vanish", product_id="777", confirm=False,
    )
    assert "PREVIEW" in out
    assert "Remove product from collection" in out
    assert "confirm=True" in out
    assert len(fc.calls) == 1


def test_remove_product_confirmed_calls_mutation_with_correct_gids():
    tools, fc = _build([_manual_collection(), _remove_ok(job_id="888")])
    out = tools["remove_product_from_collection"](
        handle="vanish", product_id="777", confirm=True,
    )
    assert out.startswith("Done.")
    assert "Removed product from collection" in out
    assert "Job        : 888" in out
    mutation_query, mutation_vars = fc.calls[1]
    assert mutation_query == REMOVE_PRODUCTS_FROM_COLLECTION
    assert mutation_vars == {
        "id": "gid://shopify/Collection/123",
        "productIds": ["gid://shopify/Product/777"],
    }


def test_remove_product_rejects_smart_collection_without_mutation():
    tools, fc = _build([_smart_collection()])
    out = tools["remove_product_from_collection"](
        handle="smart-vanish", product_id="777", confirm=True,
    )
    assert "Error:" in out
    assert "smart collection" in out
    assert len(fc.calls) == 1


def test_remove_product_collection_not_found():
    tools, fc = _build([{"collectionByHandle": None}])
    out = tools["remove_product_from_collection"](
        handle="missing", product_id="777", confirm=True,
    )
    assert out == "No collection found with handle 'missing'."


def test_remove_product_rejects_empty_product_id_without_reading_collection():
    tools, fc = _build([])
    out = tools["remove_product_from_collection"](
        handle="vanish", product_id="", confirm=True,
    )
    assert out == "Provide product_id."
    assert len(fc.calls) == 0


def test_remove_product_surfaces_user_errors():
    tools, fc = _build([
        _manual_collection(),
        {"collectionRemoveProducts": {
            "job": None,
            "userErrors": [
                {"field": ["productIds", "0"], "message": "Product not in collection"},
            ],
        }},
    ])
    out = tools["remove_product_from_collection"](
        handle="vanish", product_id="777", confirm=True,
    )
    assert out.startswith("Error:")
    assert "Product not in collection" in out


# --- job polling branches ---
#
# Covers the three outcomes of the 10s fixed-timeout poll_job helper when the
# mutation returns `done=false`:
#   1. poll flips to done=true within budget
#   2. budget exhausted, done still false (timeout)
#   3. every poll raises a transport error (poll_failed)
#
# The initial-done=true path is already covered by the *_confirmed_calls_*
# tests above (which get back `Job : 999 (done=True)` with no extra polls).


def _job_status(job_id, done):
    return {"node": {"id": f"gid://shopify/Job/{job_id}", "done": done}}


class RaisingFakeClient:
    """FakeClient variant where some responses are exceptions to be raised."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, query, variables=None):
        self.calls.append((query, variables))
        if not self.responses:
            raise AssertionError("RaisingFakeClient: unexpected extra execute() call")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_add_product_polls_job_when_initial_done_false_and_flips_true(fake_poll_clock):
    # Sequence: resolve-collection → mutation (done=false) → poll (done=true)
    tools, fc = _build([
        _manual_collection(),
        _add_ok(job_id="999", done=False),
        _job_status("999", True),
    ])
    out = tools["add_product_to_collection"](
        handle="vanish", product_id="777", confirm=True,
    )
    assert "Job        : 999 (done=True after" in out, out
    assert len(fc.calls) == 3
    assert fc.calls[2][0] == JOB_STATUS_QUERY
    assert fc.calls[2][1] == {"id": "gid://shopify/Job/999"}


def test_add_product_polling_times_out_when_done_stays_false(fake_poll_clock):
    # 1 collection read + 1 mutation + up to 11 poll calls (10s / 1s intervals).
    # We pre-load more than that so no "unexpected extra" is triggered.
    poll_responses = [_job_status("999", False)] * 20
    tools, fc = _build([
        _manual_collection(),
        _add_ok(job_id="999", done=False),
        *poll_responses,
    ])
    out = tools["add_product_to_collection"](
        handle="vanish", product_id="777", confirm=True,
    )
    assert "still running server-side after 10s timeout" in out, out
    assert "verify via get_collection" in out, out
    # At least one poll was issued.
    assert any(c[0] == JOB_STATUS_QUERY for c in fc.calls)


def test_add_product_polling_transport_error_surfaces_poll_failed_message(fake_poll_clock):
    # Replace the default FakeClient with one that raises on every poll.
    srv = CapturingServer()
    fc = RaisingFakeClient([
        _manual_collection(),
        _add_ok(job_id="999", done=False),
        *([RuntimeError("upstream 503")] * 20),
    ])
    collections.register(srv, fc)
    out = srv.tools["add_product_to_collection"](
        handle="vanish", product_id="777", confirm=True,
    )
    assert "poll failed: upstream 503" in out, out
    assert "underlying write succeeded" in out, out


def test_remove_product_polls_and_reports_elapsed_when_job_completes(fake_poll_clock):
    tools, fc = _build([
        _manual_collection(),
        _remove_ok(job_id="888", done=False),
        _job_status("888", False),
        _job_status("888", True),
    ])
    out = tools["remove_product_from_collection"](
        handle="vanish", product_id="777", confirm=True,
    )
    assert "Job        : 888 (done=True after" in out, out
    assert out.count("JobStatus") == 0  # user-facing text shouldn't leak the query name
    # Two poll calls: the first returned done=false, the second done=true.
    poll_calls = [c for c in fc.calls if c[0] == JOB_STATUS_QUERY]
    assert len(poll_calls) == 2
    assert all(c[1] == {"id": "gid://shopify/Job/888"} for c in poll_calls)


def test_initial_done_true_does_not_poll():
    tools, fc = _build([_manual_collection(), _add_ok(job_id="123", done=True)])
    out = tools["add_product_to_collection"](
        handle="vanish", product_id="777", confirm=True,
    )
    assert "Job        : 123 (done=True)" in out, out
    # Exactly 2 calls — no poll issued.
    assert len(fc.calls) == 2
    assert not any(c[0] == JOB_STATUS_QUERY for c in fc.calls)


# --- update_collection ---

def _collection_update_ok():
    return {"collectionUpdate": {
        "collection": {"id": "gid://shopify/Collection/123", "title": "new", "handle": "vanish"},
        "userErrors": [],
    }}


def _collection_update_err(field, message):
    return {"collectionUpdate": {
        "collection": None,
        "userErrors": [{"field": field, "message": message}],
    }}


def test_update_collection_requires_title_or_description_no_read():
    """Empty inputs short-circuit before any Shopify call."""
    tools, fc = _build([])
    out = tools["update_collection"](handle="vanish")
    assert out == "Provide at least one of new_title or new_description."
    assert fc.calls == []


def test_update_collection_handle_not_found():
    tools, fc = _build([{"collectionByHandle": None}])
    out = tools["update_collection"](handle="nope", new_title="X")
    assert out == "No collection found with handle 'nope'."
    # Exactly one call: the resolve-by-handle read. No mutation.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_COLLECTION_BY_HANDLE


def test_update_collection_preview_does_not_mutate():
    tools, fc = _build([_manual_collection(handle="vanish", title="Vanish")])
    out = tools["update_collection"](
        handle="vanish", new_title="New Title", new_description="<p>new</p>",
    )
    assert "PREVIEW" in out
    assert "Vanish" in out and "New Title" in out
    assert "confirm=True" in out
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_COLLECTION_BY_HANDLE


def test_update_collection_confirm_title_only_sends_only_title_field():
    tools, fc = _build([
        _manual_collection(handle="vanish", title="Vanish"),
        _collection_update_ok(),
    ])
    out = tools["update_collection"](
        handle="vanish", new_title="Renamed", confirm=True,
    )
    assert out.startswith("Done.")
    # Mutation call: input must carry id + title only — description absent.
    query, vars_ = fc.calls[1]
    assert query == UPDATE_COLLECTION
    assert vars_["input"]["id"] == "gid://shopify/Collection/123"
    assert vars_["input"]["title"] == "Renamed"
    assert "descriptionHtml" not in vars_["input"]


def test_update_collection_confirm_description_only_sends_only_description_field():
    tools, fc = _build([
        _manual_collection(handle="vanish", title="Vanish"),
        _collection_update_ok(),
    ])
    tools["update_collection"](
        handle="vanish", new_description="<p>rewritten</p>", confirm=True,
    )
    _, vars_ = fc.calls[1]
    assert vars_["input"]["descriptionHtml"] == "<p>rewritten</p>"
    assert "title" not in vars_["input"]


def test_update_collection_user_errors_surfaced():
    tools, fc = _build([
        _manual_collection(handle="vanish", title="Vanish"),
        _collection_update_err("title", "too long"),
    ])
    out = tools["update_collection"](
        handle="vanish", new_title="X" * 500, confirm=True,
    )
    assert out.startswith("Error:")
    assert "title: too long" in out
