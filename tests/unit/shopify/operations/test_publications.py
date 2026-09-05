"""
Offline unit tests for shopify.operations.publications.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the publications operations are callable from non-MCP
entry points (Story 10.30 / A5, AC4). The two product-publication reads
(GET_PRODUCT_PUBLICATIONS_BY_ID and GET_PRODUCT_PUBLICATIONS_BY_HANDLE) differ only
in their root field, so the whole shared Product selection is factored into one
ProductPublicationsFields fragment both spread (AC3 — the fragment-reuse decision
is pinned by test_shared_product_publications_fragment_is_reused).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/shopify/operations/test_publications.py -v
"""

import pytest

from shopify_mcp.shopify.operations import publications as ops
from shopify_mcp.shopify.queries import publications as q
from tests.support import FakeClient

# ---------- AC3: a shared Product selection fragment IS extracted and reused ----


def test_shared_product_publications_fragment_is_reused():
    """The id/title/handle/resourcePublications selection is identical across the
    by-id and by-handle reads, so it lives in one ProductPublicationsFields
    fragment that both GET queries spread."""
    assert "fragment ProductPublicationsFields on Product" in q.PRODUCT_PUBLICATIONS_FIELDS
    for query in (q.GET_PRODUCT_PUBLICATIONS_BY_ID, q.GET_PRODUCT_PUBLICATIONS_BY_HANDLE):
        assert "...ProductPublicationsFields" in query
        # the fragment definition is embedded in the query document it's used in
        assert q.PRODUCT_PUBLICATIONS_FIELDS.strip() in query


# ---------- read_publications (paginated list read) ----------


def test_read_publications_paginates_and_returns_nodes():
    resp = {
        "publications": {
            "nodes": [
                {"id": "gid://shopify/Publication/1", "name": "Online Store"},
                {"id": "gid://shopify/Publication/2", "name": "POS"},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    fc = FakeClient([resp])
    out = ops.read_publications(fc)
    assert out == resp["publications"]["nodes"]
    assert fc.calls[0][0] == q.LIST_PUBLICATIONS
    assert fc.calls[0][1] == {"first": 50, "after": None}
    assert ops.PUBLICATIONS_PAGE_SIZE == 50


def test_read_publications_empty_returns_empty_list():
    fc = FakeClient(
        [{"publications": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}]
    )
    assert ops.read_publications(fc) == []


# ---------- read_product_publications (by id / by handle / neither / null) ------


def _product_pubs_response(root_key: str, pid: str = "123") -> dict:
    return {
        root_key: {
            "id": f"gid://shopify/Product/{pid}",
            "title": "Tee",
            "handle": "tee",
            "resourcePublications": {
                "nodes": [
                    {
                        "publication": {
                            "id": "gid://shopify/Publication/1",
                            "name": "Online Store",
                        },
                        "isPublished": True,
                        "publishDate": "2026-01-01",
                    },
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
    }


def test_read_product_publications_by_id_coerces_gid_and_returns_product_and_rps():
    resp = _product_pubs_response("product", pid="123")
    fc = FakeClient([resp])
    product, rps, capped = ops.read_product_publications(fc, "123", "")
    assert product == resp["product"]
    assert rps == resp["product"]["resourcePublications"]["nodes"]
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCT_PUBLICATIONS_BY_ID
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/123", "first": 50, "after": None}


def test_read_product_publications_by_handle_passes_handle_var():
    resp = _product_pubs_response("productByHandle", pid="456")
    fc = FakeClient([resp])
    product, rps, capped = ops.read_product_publications(fc, "", "tee")
    assert product == resp["productByHandle"]
    assert rps == resp["productByHandle"]["resourcePublications"]["nodes"]
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCT_PUBLICATIONS_BY_HANDLE
    assert fc.calls[0][1] == {"handle": "tee", "first": 50, "after": None}


# Story 10.68 (T-10.65-refuse-both-fanout). This replaces
# `test_read_product_publications_product_id_wins_when_both_given`, which pinned
# the silent `product_id`-wins precedence this story deliberately breaks. The two
# identifiers name *different* products on purpose — the failure under test is
# "the wrong one was silently chosen", which a same-product fixture cannot show.
_S1068_DECOY_ID = "123"
_S1068_HANDLE = "some-other-product"
_S1068_DECOY_GID = f"gid://shopify/Product/{_S1068_DECOY_ID}"


def test_s1068_both_identifiers_refused_before_any_network():
    """One rule across the repo: an ambiguous identifier pair is a caller bug
    surfaced loudly, not a precedence that discards the handle in silence."""
    fc = FakeClient([])
    with pytest.raises(ValueError, match="not both"):
        ops.read_product_publications(fc, _S1068_DECOY_ID, _S1068_HANDLE)
    assert fc.calls == []


def test_s1068_both_supplied_never_reads_the_product_id_twin():
    """Under the old precedence this call read gid://shopify/Product/123 and
    dropped the handle. Prove that GID is never addressed."""
    fc = FakeClient([])
    with pytest.raises(ValueError):
        ops.read_product_publications(fc, _S1068_DECOY_ID, _S1068_HANDLE)
    assert _S1068_DECOY_GID not in str(fc.calls)


def test_read_product_publications_neither_id_nor_handle_skips_client():
    """No identifier → (None, [], False) without ever calling the client."""
    fc = FakeClient([])
    assert ops.read_product_publications(fc, "", "") == (None, [], False)
    assert fc.calls == []


def test_read_product_publications_null_product_returns_none():
    """A deleted / wrong id (Shopify returns {"product": null}) → (None, [], False)."""
    fc = FakeClient([{"product": None}])
    product, rps, capped = ops.read_product_publications(fc, "999", "")
    assert product is None
    assert rps == []
    assert capped is False


# ---------- writes: publish / unpublish build PublicationInput + execute --------


def test_publish_builds_publication_input_and_executes():
    result = {
        "publishablePublish": {
            "publishable": {"id": "gid://shopify/Product/123"},
            "userErrors": [],
        }
    }
    fc = FakeClient([result])
    out = ops.publish(
        fc,
        "gid://shopify/Product/123",
        ["gid://shopify/Publication/1", "gid://shopify/Publication/2"],
    )
    assert out == result
    assert fc.calls[0][0] == q.PUBLISHABLE_PUBLISH
    assert fc.calls[0][1] == {
        "id": "gid://shopify/Product/123",
        "input": [
            {"publicationId": "gid://shopify/Publication/1"},
            {"publicationId": "gid://shopify/Publication/2"},
        ],
    }


def test_unpublish_builds_publication_input_and_executes():
    result = {
        "publishableUnpublish": {
            "publishable": {"id": "gid://shopify/Product/123"},
            "userErrors": [],
        }
    }
    fc = FakeClient([result])
    out = ops.unpublish(fc, "gid://shopify/Product/123", ["gid://shopify/Publication/1"])
    assert out == result
    assert fc.calls[0][0] == q.PUBLISHABLE_UNPUBLISH
    assert fc.calls[0][1] == {
        "id": "gid://shopify/Product/123",
        "input": [{"publicationId": "gid://shopify/Publication/1"}],
    }


# ---------- collection publications read (Story 10.83) ----------


def test_read_collection_publications_without_a_handle_reads_nothing():
    """Backstop for non-MCP callers. The tools guard the handle themselves, so
    this branch is unreachable through them — it exists for a CLI or script
    calling the operations layer directly, mirroring the neither-supplied
    behaviour of read_product_publications."""
    fc = FakeClient([])
    col, rps, capped = ops.read_collection_publications(fc, "")
    assert (col, rps, capped) == (None, [], False)
    assert fc.calls == []


def test_read_collection_publications_returns_the_same_triple_as_its_product_sibling():
    resp = {
        "collectionByHandle": {
            "id": "gid://shopify/Collection/900",
            "title": "All (BACKUP)",
            "handle": "all-copy",
            "ruleSet": None,
            "resourcePublications": {
                "nodes": [
                    {
                        "publication": {
                            "id": "gid://shopify/Publication/1",
                            "name": "Online Store",
                        },
                        "publishDate": "2026-04-20T10:00:00Z",
                        "isPublished": True,
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
    }
    fc = FakeClient([resp])
    col, rps, capped = ops.read_collection_publications(fc, "all-copy")
    assert col["handle"] == "all-copy"
    assert len(rps) == 1
    assert capped is False
    assert fc.calls[0][0] == q.GET_COLLECTION_PUBLICATIONS_BY_HANDLE
    assert fc.calls[0][1]["handle"] == "all-copy"


def test_read_collection_publications_missing_returns_none():
    fc = FakeClient([{"collectionByHandle": None}])
    col, rps, capped = ops.read_collection_publications(fc, "nope")
    assert col is None
    assert rps == []


# ---------- GraphQL text pins (Story 10.83) ----------
#
# Every other assertion in this suite compares a query by OBJECT IDENTITY
# (`fc.calls[0][0] == q.PUBLISHABLE_PUBLISH`), which cannot notice the query's
# content changing — the fake client returns the scripted response either way.
# Deleting the Collection inline fragment, or swapping the new read's root field
# to productByHandle, left the whole offline suite green. These pin the text the
# live store actually depends on.


def test_publishable_mutations_select_both_product_and_collection():
    """The Collection inline fragment is the load-bearing new GraphQL of Story
    10.83: without it a Collection target comes back as an empty
    `publishable {}`. Nothing else in the suite would notice its removal."""
    for mutation in (q.PUBLISHABLE_PUBLISH, q.PUBLISHABLE_UNPUBLISH):
        assert "... on Product { id title }" in mutation
        assert "... on Collection { id title }" in mutation


def test_collection_publications_read_selects_the_fields_the_tools_consume():
    query = q.GET_COLLECTION_PUBLICATIONS_BY_HANDLE
    # Root field: swapping this to productByHandle is invisible to the fakes.
    assert "collectionByHandle(handle: $handle)" in query
    # `isPublished` drives the published/not-published split; `publishDate` is
    # rendered; `ruleSet` drives the smart/manual line.
    assert "isPublished" in query
    assert "publishDate" in query
    assert "publication { id name }" in query
    assert "ruleSet { appliedDisjunctively }" in query
    # The pagination variables client.paginate() drives must be declared and used.
    assert "$first: Int!" in query
    assert "$after: String" in query
    assert "resourcePublications(first: $first, after: $after)" in query
    assert "pageInfo { hasNextPage endCursor }" in query


def test_collection_publications_read_uses_the_shared_page_size():
    """Unasserted, the page size can be dropped to 1 with the suite green —
    every extra round-trip invisible offline and expensive against the API."""
    from shopify_mcp.shopify.operations.publications import PUBLICATIONS_PAGE_SIZE

    fc = FakeClient([{"collectionByHandle": None}])
    ops.read_collection_publications(fc, "all-copy")
    assert fc.calls[0][1]["first"] == PUBLICATIONS_PAGE_SIZE


def test_collection_publications_query_parses_and_has_the_shape_paginate_walks():
    """Parse the query instead of grepping it.

    Story 10.76's lesson: substring assertions catch an UNCONVERTED query but
    not a MIS-converted one. Hoisting `pageInfo` out of the walked connection up
    one level keeps every asserted token present, leaves the suite green, and
    breaks pagination against the real API. So walk the AST down the same
    `connection_path` the runtime drives and require pageInfo to be there.

    `graphql-core` is `gql`'s own dependency and is pinned in both lockfiles, so
    this costs nothing to import.
    """
    from graphql import parse

    doc = parse(q.GET_COLLECTION_PUBLICATIONS_BY_HANDLE)
    (operation,) = doc.definitions

    def _field(selection_set, name):
        for sel in selection_set.selections:
            if getattr(sel, "name", None) is not None and sel.name.value == name:
                return sel
        raise AssertionError(f"{name!r} not found in selection set")

    # The exact path shopify_mcp.client.paginate is told to walk.
    root = _field(operation.selection_set, "collectionByHandle")
    connection = _field(root.selection_set, "resourcePublications")

    page_info = _field(connection.selection_set, "pageInfo")
    names = {s.name.value for s in page_info.selection_set.selections}
    assert {"hasNextPage", "endCursor"} <= names

    nodes = _field(connection.selection_set, "nodes")
    node_names = {s.name.value for s in nodes.selection_set.selections}
    assert {"publication", "publishDate", "isPublished"} <= node_names

    # The connection must carry the pagination arguments, not hardcode a count.
    args = {a.name.value for a in connection.arguments}
    assert args == {"first", "after"}
