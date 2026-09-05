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
  pytest tests/unit/shopify/operations/test_collections.py -v
"""

from shopify_mcp.shopify.operations import collections as ops
from shopify_mcp.shopify.queries import collections as q
from tests.support import FakeClient

# ---------- AC3: collections has no by-id/by-handle pair → no shared fragment ----------


def test_no_shared_fragment_for_collections():
    """collections has only a by-handle read (no by-id twin), so no duplicated
    selection set exists to factor out — no GraphQL fragment is defined."""
    for query in (
        q.GET_COLLECTION_BY_HANDLE,
        q.CREATE_COLLECTION,
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


# ---------- create (Story 10.82 / T-collection-create) ----------
#
# Decision 1 (manual-only) and the publishing scope guard are both structural:
# the input this layer builds must never carry `ruleSet` or `publications`, and
# there is no parameter through which a caller could supply either.


def _create_ok(handle="grey-casualty"):
    return {
        "collectionCreate": {
            "collection": {
                "id": "gid://shopify/Collection/9",
                "title": "Grey Casualty",
                "handle": handle,
            },
            "userErrors": [],
        }
    }


def test_create_collection_title_only_sends_title_field_alone():
    """Decision 2: an omitted handle is not sent at all, so Shopify derives it."""
    fc = FakeClient([_create_ok()])
    ops.create_collection(fc, title="Grey Casualty")
    assert fc.calls[0][0] == q.CREATE_COLLECTION
    assert fc.calls[0][1]["input"] == {"title": "Grey Casualty"}


def test_create_collection_sends_handle_when_supplied():
    fc = FakeClient([_create_ok()])
    ops.create_collection(fc, title="Grey Casualty", handle="grey-casualty")
    assert fc.calls[0][1]["input"] == {
        "title": "Grey Casualty",
        "handle": "grey-casualty",
    }


def test_create_collection_sends_description_when_supplied():
    fc = FakeClient([_create_ok()])
    ops.create_collection(fc, title="Grey Casualty", description_html="<p>tees</p>")
    assert fc.calls[0][1]["input"] == {
        "title": "Grey Casualty",
        "descriptionHtml": "<p>tees</p>",
    }


def test_create_collection_sends_explicitly_empty_description():
    """None means "not provided"; "" is a real value and must reach the input.

    Mirrors ops.update_collection's documented convention, so a description
    the sanitizer reduces to "" is still written rather than silently dropped.
    """
    fc = FakeClient([_create_ok()])
    ops.create_collection(fc, title="Grey Casualty", description_html="")
    assert fc.calls[0][1]["input"]["descriptionHtml"] == ""


def test_create_collection_never_sends_rule_set_or_publications():
    """Decisions 1 and the publishing scope guard, pinned at the input level."""
    fc = FakeClient([_create_ok()])
    ops.create_collection(
        fc, title="Grey Casualty", handle="grey-casualty", description_html="<p>x</p>"
    )
    inp = fc.calls[0][1]["input"]
    assert "ruleSet" not in inp
    assert "publications" not in inp
    assert set(inp) == {"title", "handle", "descriptionHtml"}


def test_create_collection_returns_raw_mutation_result():
    fc = FakeClient([_create_ok()])
    result = ops.create_collection(fc, title="Grey Casualty")
    assert result["collectionCreate"]["collection"]["handle"] == "grey-casualty"


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
