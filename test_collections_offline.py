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
)


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    """Keep tests from polluting aon_mcp_log.txt."""
    monkeypatch.setattr(collections, "log_write", lambda *a, **k: None)


class CapturingServer:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, query, variables=None):
        self.calls.append((query, variables))
        if not self.responses:
            raise AssertionError("FakeClient: unexpected extra execute() call")
        return self.responses.pop(0)


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
# documented in the Shopify Admin API 2024-10 schema:
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
