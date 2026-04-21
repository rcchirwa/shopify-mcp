"""
Offline unit tests for tools/collections.py read paths.

Uses a scripted FakeClient to exercise get_collection response unwrap
without hitting Shopify.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_collections_offline.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from tools import collections
from tools.collections import GET_COLLECTION_BY_HANDLE


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
