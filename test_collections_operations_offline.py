"""
Offline unit tests for shopify.operations.collections.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the operations are callable from non-MCP entry
points (Story 10.26 / A5, AC4).

Unlike products / catalog_hygiene, collections has a single by-handle read and
no by-id twin, so there is no duplicated selection set to factor into a shared
fragment. test_no_shared_fragment_for_collections pins that explicit decision
(AC3 — no forced fragment).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_collections_operations_offline.py -v
"""

from _testing import FakeClient
from shopify.operations import collections as ops
from shopify.queries import collections as q

# ---------- AC3: collections has no by-id/by-handle pair → no shared fragment ----------


def test_no_shared_fragment_for_collections():
    """collections has only a by-handle read (no by-id twin), so no duplicated
    selection set exists to factor out — no GraphQL fragment is defined."""
    for query in (
        q.GET_COLLECTION_BY_HANDLE,
        q.UPDATE_COLLECTION,
        q.ADD_PRODUCTS_TO_COLLECTION,
        q.REMOVE_PRODUCTS_FROM_COLLECTION,
    ):
        assert "fragment " not in query


# ---------- reads ----------


def test_read_collection_by_handle_returns_node():
    col = {"id": "gid://shopify/Collection/123", "title": "Vanish", "handle": "vanish"}
    fc = FakeClient([{"collectionByHandle": col}])
    got = ops.read_collection_by_handle(fc, "vanish")
    assert got == col
    assert fc.calls[0][0] == q.GET_COLLECTION_BY_HANDLE
    assert fc.calls[0][1] == {"handle": "vanish"}


def test_read_collection_by_handle_missing_returns_none():
    fc = FakeClient([{"collectionByHandle": None}])
    assert ops.read_collection_by_handle(fc, "nope") is None
    assert fc.calls[0][0] == q.GET_COLLECTION_BY_HANDLE


def test_read_collection_by_handle_empty_data_returns_none():
    fc = FakeClient([{}])
    assert ops.read_collection_by_handle(fc, "nope") is None


# ---------- update (input-building) ----------


def test_update_collection_title_only_builds_input():
    fc = FakeClient([{"collectionUpdate": {"collection": {"id": "g"}, "userErrors": []}}])
    result = ops.update_collection(fc, "gid://shopify/Collection/1", new_title="Renamed")
    assert result["collectionUpdate"]["collection"]["id"] == "g"
    assert fc.calls[0][0] == q.UPDATE_COLLECTION
    assert fc.calls[0][1]["input"] == {"id": "gid://shopify/Collection/1", "title": "Renamed"}


def test_update_collection_description_only_builds_input():
    fc = FakeClient([{"collectionUpdate": {"userErrors": []}}])
    ops.update_collection(fc, "gid://shopify/Collection/1", new_description="<p>x</p>")
    assert fc.calls[0][1]["input"] == {
        "id": "gid://shopify/Collection/1",
        "descriptionHtml": "<p>x</p>",
    }


def test_update_collection_both_fields_builds_input():
    fc = FakeClient([{"collectionUpdate": {"userErrors": []}}])
    ops.update_collection(
        fc, "gid://shopify/Collection/1", new_title="T", new_description="<p>x</p>"
    )
    assert fc.calls[0][1]["input"] == {
        "id": "gid://shopify/Collection/1",
        "title": "T",
        "descriptionHtml": "<p>x</p>",
    }


def test_update_collection_no_fields_sends_id_only():
    """Defensive: the tool layer guards against empty updates, but as a
    standalone op an id-only input is what an empty change produces."""
    fc = FakeClient([{"collectionUpdate": {"userErrors": []}}])
    ops.update_collection(fc, "gid://shopify/Collection/1")
    assert fc.calls[0][1]["input"] == {"id": "gid://shopify/Collection/1"}


# ---------- membership writes (GID coercion + direction-correct mutation) ----------


def test_add_products_to_collection_coerces_gid_and_uses_add_mutation():
    fc = FakeClient([{"collectionAddProductsV2": {"job": {"id": "g", "done": True}}}])
    result = ops.add_products_to_collection(fc, "gid://shopify/Collection/123", "777")
    assert result["collectionAddProductsV2"]["job"]["id"] == "g"
    assert fc.calls[0][0] == q.ADD_PRODUCTS_TO_COLLECTION
    assert fc.calls[0][1] == {
        "id": "gid://shopify/Collection/123",
        "productIds": ["gid://shopify/Product/777"],
    }


def test_remove_products_from_collection_coerces_gid_and_uses_remove_mutation():
    fc = FakeClient([{"collectionRemoveProducts": {"job": {"id": "g", "done": True}}}])
    ops.remove_products_from_collection(fc, "gid://shopify/Collection/123", "888")
    assert fc.calls[0][0] == q.REMOVE_PRODUCTS_FROM_COLLECTION
    assert fc.calls[0][1] == {
        "id": "gid://shopify/Collection/123",
        "productIds": ["gid://shopify/Product/888"],
    }
