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

import graphql
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


def _v2_response(nodes: list, has_next: bool = False, end_cursor: str | None = None) -> dict:
    """One page of GET_PRODUCT_ASSIGNED_PUBLICATIONS (Story 10.99)."""
    return {
        "product": {
            "resourcePublicationsV2": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
            }
        }
    }


def _rp(pid: str, is_published: bool) -> dict:
    return {
        "publication": {"id": f"gid://shopify/Publication/{pid}", "name": f"Channel {pid}"},
        "isPublished": is_published,
        "publishDate": "2026-01-01" if is_published else None,
    }


def test_read_product_publications_by_id_coerces_gid_and_returns_product_and_rps():
    resp = _product_pubs_response("product", pid="123")
    fc = FakeClient([resp, _v2_response([])])
    product, rps, capped = ops.read_product_publications(fc, "123", "")
    assert product == resp["product"]
    assert rps == resp["product"]["resourcePublications"]["nodes"]
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCT_PUBLICATIONS_BY_ID
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/123", "first": 50, "after": None}
    assert fc.calls[1][0] == q.GET_PRODUCT_ASSIGNED_PUBLICATIONS
    assert fc.calls[1][1] == {"id": "gid://shopify/Product/123", "first": 50, "after": None}


def test_read_product_publications_by_handle_passes_handle_var():
    resp = _product_pubs_response("productByHandle", pid="456")
    fc = FakeClient([resp, _v2_response([])])
    product, rps, capped = ops.read_product_publications(fc, "", "tee")
    assert product == resp["productByHandle"]
    assert rps == resp["productByHandle"]["resourcePublications"]["nodes"]
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCT_PUBLICATIONS_BY_HANDLE
    assert fc.calls[0][1] == {"handle": "tee", "first": 50, "after": None}
    # The V2 read goes by the resolved product's id, not the handle.
    assert fc.calls[1][0] == q.GET_PRODUCT_ASSIGNED_PUBLICATIONS
    assert fc.calls[1][1] == {"id": "gid://shopify/Product/456", "first": 50, "after": None}


# ---------- Story 10.99: v1 is the published source, V2 adds assigned records ----


def test_s1099_merge_adds_only_v2_assigned_records_that_v1_lacks():
    """v1 is authoritative for published: V2's isPublished:true records are
    ignored even when v1 lacks them, and a V2 record for a channel v1 already
    lists is not added twice. Only V2's isPublished:false records for channels
    v1 lacks are appended, after the v1 records."""
    v1 = _product_pubs_response("product")  # Publication/1, published
    v1_nodes = v1["product"]["resourcePublications"]["nodes"]
    v2 = _v2_response(
        [
            _rp("1", False),  # v1 lists it: v1 wins, not added
            _rp("5", True),  # published per V2 only: ignored
            _rp("4", False),  # assigned: added
        ]
    )
    fc = FakeClient([v1, v2])
    product, rps, capped = ops.read_product_publications(fc, "123", "")
    assert product == v1["product"]
    assert rps == [*v1_nodes, _rp("4", False)]
    assert capped is False
    assert fc.responses == []


def test_s1099_merge_walks_every_v2_page():
    v1 = _product_pubs_response("product")
    page0 = _v2_response([_rp("3", False)], has_next=True, end_cursor="v2c")
    page1 = _v2_response([_rp("4", False)])
    fc = FakeClient([v1, page0, page1])
    _product, rps, capped = ops.read_product_publications(fc, "123", "")
    assert [rp["publication"]["id"] for rp in rps] == [
        "gid://shopify/Publication/1",
        "gid://shopify/Publication/3",
        "gid://shopify/Publication/4",
    ]
    assert capped is False
    assert fc.calls[2][1] == {"id": "gid://shopify/Product/123", "first": 50, "after": "v2c"}


def test_s1099_capped_is_true_when_only_the_v2_walk_stops_short():
    """hasNextPage with a null endCursor stops the V2 walk short; the v1 walk
    completed, so only V2 can make capped True here."""
    v1 = _product_pubs_response("product")
    fc = FakeClient([v1, _v2_response([_rp("4", False)], has_next=True, end_cursor=None)])
    _product, rps, capped = ops.read_product_publications(fc, "123", "")
    assert capped is True
    assert len(rps) == 2


def test_s1099_capped_is_true_when_only_the_v1_walk_stops_short():
    v1 = _product_pubs_response("product")
    v1["product"]["resourcePublications"]["pageInfo"] = {"hasNextPage": True, "endCursor": None}
    fc = FakeClient([v1, _v2_response([])])
    _product, _rps, capped = ops.read_product_publications(fc, "123", "")
    assert capped is True


@pytest.mark.parametrize(
    ("product_id", "handle", "root_key"), [("999", "", "product"), ("", "gone", "productByHandle")]
)
def test_s1099_an_unresolved_product_makes_no_v2_read(product_id, handle, root_key):
    fc = FakeClient([{root_key: None}])
    assert ops.read_product_publications(fc, product_id, handle) == (None, [], False)
    assert len(fc.calls) == 1


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


def _field(selection_set: graphql.SelectionSetNode, name: str) -> graphql.FieldNode:
    """Return the field named `name` in `selection_set`, requiring it unaliased.

    Module level, not a closure, so test_publications_walker_rejects_an_aliased_field
    can drive it directly against a literal query (Story 10.84).

    The alias assertion is what makes this a mirror of client.paginate(), which
    descends the RESPONSE by key — and a field's response key is its alias when
    one is present, its name otherwise. Matching on the name alone leaves an
    alias undetected: valid GraphQL, every other assertion here still true, and
    paginate() reading {} for a connection that is really there.

    The isinstance check is load-bearing, not decoration: FragmentSpreadNode
    also carries a `.name` and has no `.alias` at all, so the pre-10.84 duck-typed
    predicate would turn the assertion below into an AttributeError the moment a
    fragment happened to share a walked field's name.
    """
    for sel in selection_set.selections:
        if isinstance(sel, graphql.FieldNode) and sel.name.value == name:
            assert sel.alias is None, (
                f"{name!r} is aliased to {sel.alias.value!r}; paginate() reads the "
                f"response key, which would be {sel.alias.value!r}"
            )
            return sel
    raise AssertionError(f"{name!r} not found in selection set")


def _field_names(selection_set: graphql.SelectionSetNode, where: str) -> set[str]:
    """Return the field names in `selection_set`, requiring every one unaliased.

    The leaf twin of _field, and the same rule: paginate() reads `hasNextPage`
    and `endCursor` out of pageInfo by key, so a name-keyed set built off these
    selections cannot see an alias on them. Aliasing hasNextPage stops the walk
    after one page and reports capped=False — a truncation presented as a
    complete answer. Caught today only by a SUBSTRING pin, which this repo has
    now learned three times over must not be the control (Stories 10.76, 10.78).
    """
    names: set[str] = set()
    for sel in selection_set.selections:
        if not isinstance(sel, graphql.FieldNode):
            continue
        assert sel.alias is None, (
            f"{where}: {sel.name.value!r} is aliased to {sel.alias.value!r}; the response "
            f"key would be {sel.alias.value!r}"
        )
        names.add(sel.name.value)
    return names


def test_publications_field_names_rejects_an_aliased_leaf():
    """The leaf guard has teeth, pinned against a literal query."""
    doc = graphql.parse("query Q { c { rp { pageInfo { hn: hasNextPage endCursor } } } }")
    (operation,) = doc.definitions
    collection = _field(operation.selection_set, "c")
    connection = _field(collection.selection_set, "rp")
    page_info = _field(connection.selection_set, "pageInfo")
    with pytest.raises(AssertionError, match="aliased"):
        _field_names(page_info.selection_set, "pageInfo")


def test_publications_walker_rejects_an_aliased_field():
    """AC4 (Story 10.84): the walker refuses an alias on any step of the path.

    Same defect and same reasoning as test_selection_at_rejects_an_aliased_field
    in test_products.py — the response key for a field is its alias when one is
    present, so a name-matching walker cannot claim to mirror paginate().
    Pinned against a literal query so it survives any rewrite of the real ones.
    """
    aliased = """
    query Q($first: Int!, $after: String) {
      x: resourcePublications(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    (operation,) = graphql.parse(aliased).definitions
    with pytest.raises(AssertionError, match="aliased"):
        _field(operation.selection_set, "resourcePublications")


def test_product_publications_fragment_reads_v1_resource_publications():
    """Story 10.99: v1 stays the published source. resourcePublicationsV2
    omits published channels that are not in the publications roster (Meta,
    Microsoft Copilot), which v1 returns (live, 2026-09-26, API 2026-01)."""
    (fragment,) = graphql.parse(q.PRODUCT_PUBLICATIONS_FIELDS).definitions
    connection = _field(fragment.selection_set, "resourcePublications")
    assert {a.name.value for a in connection.arguments} == {"first", "after"}
    names = {s.name.value for s in fragment.selection_set.selections}
    assert "resourcePublicationsV2" not in names


def test_assigned_publications_query_reads_v2_including_unpublished_records():
    """Story 10.99: only resourcePublicationsV2(onlyPublished: false) shows a
    DRAFT product's assigned channel (isPublished false). resourcePublications
    hides it whatever onlyPublished is set to (live, 2026-09-26, API 2026-01).
    Walked down the connection_path the operation passes to paginate()."""
    (operation,) = graphql.parse(q.GET_PRODUCT_ASSIGNED_PUBLICATIONS).definitions
    root = _field(operation.selection_set, "product")
    assert {a.name.value: a.value.name.value for a in root.arguments} == {"id": "id"}
    connection = _field(root.selection_set, "resourcePublicationsV2")

    args = {a.name.value: a.value for a in connection.arguments}
    assert set(args) == {"first", "after", "onlyPublished"}
    assert isinstance(args["onlyPublished"], graphql.BooleanValueNode)
    assert args["onlyPublished"].value is False
    assert isinstance(args["first"], graphql.VariableNode) and args["first"].name.value == "first"
    assert isinstance(args["after"], graphql.VariableNode) and args["after"].name.value == "after"

    page_info = _field(connection.selection_set, "pageInfo")
    assert {"hasNextPage", "endCursor"} <= _field_names(page_info.selection_set, "pageInfo")
    nodes = _field(connection.selection_set, "nodes")
    node_names = _field_names(nodes.selection_set, "nodes")
    assert {"publication", "publishDate", "isPublished"} <= node_names


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
    doc = graphql.parse(q.GET_COLLECTION_PUBLICATIONS_BY_HANDLE)
    (operation,) = doc.definitions

    # The exact path shopify_mcp.client.paginate is told to walk.
    root = _field(operation.selection_set, "collectionByHandle")
    connection = _field(root.selection_set, "resourcePublications")

    page_info = _field(connection.selection_set, "pageInfo")
    names = _field_names(page_info.selection_set, "pageInfo")
    assert {"hasNextPage", "endCursor"} <= names

    nodes = _field(connection.selection_set, "nodes")
    node_names = _field_names(nodes.selection_set, "nodes")
    assert {"publication", "publishDate", "isPublished"} <= node_names

    # The connection must carry the pagination arguments, not hardcode a count.
    args = {a.name.value for a in connection.arguments}
    assert args == {"first", "after"}


def test_product_publications_fragment_selects_the_product_status():
    """Story 10.99: publishing to a DRAFT product only assigns the channel, so
    the tools need the status to mark it (live-found 2026-09-26)."""
    (fragment,) = graphql.parse(q.PRODUCT_PUBLICATIONS_FIELDS).definitions
    assert "status" in _field_names(fragment.selection_set, "ProductPublicationsFields")
