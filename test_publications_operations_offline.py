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
  pytest test_publications_operations_offline.py -v
"""

from _testing import FakeClient
from shopify.operations import publications as ops
from shopify.queries import publications as q

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


def test_read_product_publications_product_id_wins_when_both_given():
    """Mirrors the prior tool behavior: when both identifiers are supplied the
    by-id read is taken (handle ignored), so only the by-id query fires."""
    resp = _product_pubs_response("product", pid="123")
    fc = FakeClient([resp])
    ops.read_product_publications(fc, "123", "tee")
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == q.GET_PRODUCT_PUBLICATIONS_BY_ID


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
