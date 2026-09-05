"""
Offline unit tests for shopify.operations.products.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the operations are callable from non-MCP entry
points (Story 10.23 / A5, AC4). They also pin the shared-fragment reuse across
the by-id and by-handle product queries (AC3).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/shopify/operations/test_products.py -v
"""

from typing import Any

import graphql
import pytest

from shopify_mcp.shopify.operations import products as ops
from shopify_mcp.shopify.queries import products as q
from tests.support import FakeClient, collection_products_page, products_page

# ---------- AC3: shared GraphQL fragment reused across by-id / by-handle ----------


def test_core_fragment_defined_once_and_reused():
    """GET_PRODUCT_BY_ID and GET_PRODUCT_BY_HANDLE both spread the same
    product-core fragment instead of duplicating the selection set."""
    assert "fragment ProductCoreFields on Product" in q.GET_PRODUCT_BY_ID
    assert "fragment ProductCoreFields on Product" in q.GET_PRODUCT_BY_HANDLE
    assert "...ProductCoreFields" in q.GET_PRODUCT_BY_ID
    assert "...ProductCoreFields" in q.GET_PRODUCT_BY_HANDLE


def test_full_fragment_defined_once_and_reused():
    """The full-record by-id / by-handle pair shares a fragment too."""
    assert "fragment ProductFullFields on Product" in q.GET_PRODUCT_FULL_BY_ID
    assert "fragment ProductFullFields on Product" in q.GET_PRODUCT_FULL_BY_HANDLE
    assert "...ProductFullFields" in q.GET_PRODUCT_FULL_BY_ID
    assert "...ProductFullFields" in q.GET_PRODUCT_FULL_BY_HANDLE


# ---------- discriminator guard (non-MCP callers must pass id or handle) ----------


@pytest.mark.parametrize(
    "op",
    [ops.read_product, ops.read_product_full, ops.read_product_description],
)
def test_read_ops_require_a_discriminator(op):
    fc = FakeClient([])  # no responses: guard must fire before any execute()
    with pytest.raises(ValueError, match="provide either product_id or handle"):
        op(fc)
    assert fc.calls == [], "guard must raise before issuing any query"


# ---------- read operations ----------


def test_read_products_returns_nodes():
    fc = FakeClient([products_page([{"id": "gid://shopify/Product/1"}])])
    nodes, capped = ops.read_products(fc)
    assert nodes == [{"id": "gid://shopify/Product/1"}]
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCTS
    # query=None (no status filter), the nested variant cap read from the single
    # operations-layer constant (Story 10.77), plus the cursor pair paginate()
    # supplies. Asserted as an exact dict so a variable that stops being bound —
    # and would then fall to whatever default the query declares, or fail live —
    # cannot slip through.
    assert fc.calls[0][1] == {
        "query": None,
        "variantsFirst": ops.GET_PRODUCTS_VARIANT_CAP,
        "first": ops.PRODUCTS_PAGE_SIZE,
        "after": None,
    }


def test_read_products_empty():
    fc = FakeClient([{}])
    assert ops.read_products(fc) == ([], False)


# ---------- Story 10.72: outer-connection pagination, status filter, limit ----------


def test_get_products_query_has_the_shape_paginate_requires():
    """The query text is the one artifact FakeClient cannot validate — it never
    parses the GraphQL and the transport is built with
    fetch_schema_from_transport=False, so reverting GET_PRODUCTS to its
    pre-10.72 single-shot form would leave every behavioural test green while
    the feature was silently un-implemented. Pin the shape directly, the way
    Story 10.34 pins GET_ORDERS' nested pageInfo."""
    assert "$after: String" in q.GET_PRODUCTS
    assert "after: $after" in q.GET_PRODUCTS
    assert "$query: String" in q.GET_PRODUCTS
    assert "query: $query" in q.GET_PRODUCTS
    assert "pageInfo" in q.GET_PRODUCTS
    assert "hasNextPage" in q.GET_PRODUCTS
    assert "endCursor" in q.GET_PRODUCTS


# ---------- Story 10.77: the nested variants connection is detectable ----------


def test_get_products_nested_variants_connection_selects_page_info():
    """The nested variants connection must select pageInfo { hasNextPage }: it is
    the only thing in the response that can say a product's variant list was cut
    short, and without it the truncation is structurally undetectable — the state
    this story exists to end.

    Parsed rather than substring-matched, for the reason Story 10.76 recorded and
    Story 10.83 repeated: a substring check passes as long as the tokens appear
    SOMEWHERE, so hoisting pageInfo out of the nested connection up onto the
    product node keeps every asserted token present while restoring the silence.
    """
    variants = _selection_at(q.GET_PRODUCTS, ["products", "nodes", "variants"])
    fields = {sel.name.value for sel in variants.selections if isinstance(sel, graphql.FieldNode)}
    assert "pageInfo" in fields, f"pageInfo is not on the nested variants connection: {fields}"

    page_info = _selection_at(q.GET_PRODUCTS, ["products", "nodes", "variants", "pageInfo"])
    page_info_fields = {
        sel.name.value for sel in page_info.selections if isinstance(sel, graphql.FieldNode)
    }
    assert "hasNextPage" in page_info_fields, page_info_fields


def test_get_products_nested_variant_cap_is_a_bound_variable_not_a_literal():
    """The cap must reach the query as $variantsFirst, sourced from
    GET_PRODUCTS_VARIANT_CAP, so the number in the query and the number in the
    warning copy cannot drift — the single-source-of-truth property the
    GET_ORDERS_LINE_ITEM_CAP precedent names explicitly."""
    nodes_selection = _selection_at(q.GET_PRODUCTS, ["products", "nodes"])
    variants_field = next(
        sel
        for sel in nodes_selection.selections
        if isinstance(sel, graphql.FieldNode) and sel.name.value == "variants"
    )
    # An alias would change the RESPONSE key while leaving sel.name.value at
    # "variants", so every other assertion here passes while p.get("variants")
    # returns None for every node — detection dead, variant lists blank. Found
    # by the story's adversarial verifier as a survivor of the parse guard.
    assert variants_field.alias is None, (
        f"variants is aliased to {variants_field.alias.value!r}; the response key would not "
        "be 'variants' and both the render loop and capped_variant_product_ids would miss it"
    )

    args = {a.name.value: a.value for a in variants_field.arguments}
    assert isinstance(args["first"], graphql.VariableNode), (
        "variants(first:) is still a literal — the query and the warning copy can drift"
    )
    assert args["first"].name.value == "variantsFirst"

    definitions = {
        v.variable.name.value: v
        for v in graphql.parse(q.GET_PRODUCTS).definitions[0].variable_definitions
    }
    assert "variantsFirst" in definitions, (
        f"GET_PRODUCTS does not declare $variantsFirst: {sorted(definitions)}"
    )
    # Int!, not Int and not Int = 50. A nullable declaration, or one carrying a
    # default, gives the query a second place the cap could come from — which is
    # the exact drift this story exists to close.
    assert graphql.print_ast(definitions["variantsFirst"].type) == "Int!"
    assert definitions["variantsFirst"].default_value is None


def _node_with_variant_page(pid: str, *, has_next: bool) -> dict[str, Any]:
    """One product node carrying the nested variants connection's pageInfo."""
    return {
        "id": f"gid://shopify/Product/{pid}",
        "variants": {"nodes": [], "pageInfo": {"hasNextPage": has_next}},
    }


def test_capped_variant_product_ids_flags_products_past_the_cap():
    """A product whose variants.pageInfo.hasNextPage is True is reported capped,
    named by its gid (the tool layer applies from_gid for display)."""
    products = [_node_with_variant_page("111", has_next=True)]
    assert ops.capped_variant_product_ids(products) == ["gid://shopify/Product/111"]


def test_capped_variant_product_ids_empty_when_within_cap():
    products = [_node_with_variant_page("111", has_next=False)]
    assert ops.capped_variant_product_ids(products) == []


def test_capped_variant_product_ids_treats_missing_shapes_as_not_capped():
    """Mixed batch: only the over-cap product's id comes back. Every degenerate
    shape is treated as not-capped — the same tolerance
    capped_line_item_order_ids has, defensive against permissions-trimmed
    responses.

    The null-pageInfo case is the one that pins the SECOND `or {}` in the chain.
    Without it, weakening that link to `.get("pageInfo", {})` left the whole
    suite green while raising AttributeError on this exact input."""
    products = [
        _node_with_variant_page("111", has_next=True),
        _node_with_variant_page("222", has_next=False),
        {"id": "gid://shopify/Product/333", "variants": {"nodes": []}},  # no pageInfo
        {"id": "gid://shopify/Product/444", "variants": None},  # null connection
        {"id": "gid://shopify/Product/555"},  # variants absent entirely
        {"id": "gid://shopify/Product/666", "variants": {"pageInfo": None}},  # null pageInfo
        {"id": "gid://shopify/Product/777", "variants": {"pageInfo": {"hasNextPage": None}}},
    ]
    assert ops.capped_variant_product_ids(products) == ["gid://shopify/Product/111"]


def test_capped_variant_product_ids_returns_every_capped_id_in_order():
    """One id per capped product, in the order the products arrived — not just
    the first, not deduplicated into a set, not reordered. Truncating, reversing
    or sorting the comprehension all left the suite green until this existed.

    The ids are deliberately NOT in ascending order: with an ascending fixture,
    routing the result through `sorted(set(...))` is indistinguishable from
    preserving page order, and that mutation survived."""
    products = [
        _node_with_variant_page("333", has_next=True),
        _node_with_variant_page("222", has_next=False),
        _node_with_variant_page("111", has_next=True),
        _node_with_variant_page("444", has_next=True),
    ]
    assert ops.capped_variant_product_ids(products) == [
        "gid://shopify/Product/333",
        "gid://shopify/Product/111",
        "gid://shopify/Product/444",
    ]


def test_capped_variant_product_ids_empty_for_empty_batch():
    assert ops.capped_variant_product_ids([]) == []


def test_read_products_follows_cursor_across_pages():
    """AC1: a first page reporting hasNextPage=True yields the SECOND page's
    products too, not just the first."""
    fc = FakeClient(
        [
            products_page([{"id": "gid://shopify/Product/1"}], has_next=True, cursor="C1"),
            products_page([{"id": "gid://shopify/Product/2"}]),
        ]
    )
    nodes, capped = ops.read_products(fc)
    assert [n["id"] for n in nodes] == ["gid://shopify/Product/1", "gid://shopify/Product/2"]
    assert capped is False
    # The second request carried the first page's endCursor.
    assert fc.calls[1][1]["after"] == "C1"
    # Story 10.77: and EVERY page carries the variant cap, not just the first —
    # paginate() rebuilds the variables dict per page, so a cap passed anywhere
    # other than read_products' own variables would be dropped after page one.
    assert [c[1]["variantsFirst"] for c in fc.calls] == [ops.GET_PRODUCTS_VARIANT_CAP] * 2


def test_read_products_capped_when_page_budget_exhausted():
    """AC2: exhausting the page budget sets capped rather than silently
    returning a prefix."""
    pages = [
        products_page([{"id": f"gid://shopify/Product/{i}"}], has_next=True, cursor=f"C{i}")
        for i in range(ops.PRODUCTS_MAX_PAGES)
    ]
    fc = FakeClient(pages)
    nodes, capped = ops.read_products(fc)
    assert capped is True
    assert len(nodes) == ops.PRODUCTS_MAX_PAGES
    assert len(fc.calls) == ops.PRODUCTS_MAX_PAGES


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        ("ACTIVE", "status:ACTIVE"),
        ("DRAFT", "status:DRAFT"),
        ("ARCHIVED", "status:ARCHIVED"),
        ("UNLISTED", "status:UNLISTED"),
    ],
)
def test_read_products_maps_status_to_fixed_fragment(status, fragment):
    """AC3/AC4: each supported status narrows the connection through a fixed
    fragment looked up by key — the argument is never interpolated. UNLISTED
    joined the table in Story 10.74."""
    fc = FakeClient([products_page([])])
    ops.read_products(fc, status=status)
    assert fc.calls[0][1]["query"] == fragment


def test_read_products_refuses_unmapped_status_before_any_call():
    """AC3: an unrecognized status is refused before the transport fires, and
    the rejection does not echo the caller's value back."""
    fc = FakeClient([products_page([])])
    with pytest.raises(ValueError) as exc:
        ops.read_products(fc, status="ACTIVE OR status:DRAFT")
    assert fc.calls == []
    assert "ACTIVE OR" not in str(exc.value)


def test_read_products_limit_caps_results_and_request_budget():
    """A caller limit bounds both the returned list and the number of pages
    requested, and reports capped when more products exist beyond it."""
    fc = FakeClient(
        [
            products_page(
                [{"id": f"gid://shopify/Product/{i}"} for i in range(3)],
                has_next=True,
                cursor="C1",
            )
        ]
    )
    nodes, capped = ops.read_products(fc, limit=3)
    assert len(nodes) == 3
    assert capped is True
    assert len(fc.calls) == 1
    assert fc.calls[0][1]["first"] == 3


def test_read_products_limit_above_page_size_trims_the_overshoot():
    """A limit that is not a whole number of pages still returns exactly the
    limit, and reports capped because the trimmed overshoot proves more exist."""
    page = products_page(
        [{"id": f"gid://shopify/Product/{i}"} for i in range(ops.PRODUCTS_PAGE_SIZE)],
        has_next=True,
        cursor="C1",
    )
    tail = products_page([{"id": "gid://shopify/Product/tail"}] * 2)
    fc = FakeClient([page, tail])
    limit = ops.PRODUCTS_PAGE_SIZE + 1
    nodes, capped = ops.read_products(fc, limit=limit)
    assert len(nodes) == limit
    assert capped is True
    assert len(fc.calls) == 2


def test_read_products_limit_cannot_widen_the_request_budget():
    """limit narrows the page budget but must never widen it. Without the
    min() clamp a model-supplied limit sets max_pages directly, so limit=10**9
    would authorise four million sequential requests."""
    pages = [
        products_page(
            [{"id": f"gid://shopify/Product/{i}"}] * ops.PRODUCTS_PAGE_SIZE,
            has_next=True,
            cursor=f"C{i}",
        )
        for i in range(ops.PRODUCTS_MAX_PAGES)
    ]
    fc = FakeClient(pages)
    nodes, capped = ops.read_products(fc, limit=10**9)
    assert len(fc.calls) == ops.PRODUCTS_MAX_PAGES, "limit must not buy extra requests"
    assert len(nodes) == ops.PRODUCTS_PAGE_SIZE * ops.PRODUCTS_MAX_PAGES
    assert capped is True


def test_read_products_combines_status_filter_with_limit():
    """The two new arguments compose: the filter narrows the connection while
    the limit bounds the walk."""
    fc = FakeClient(
        [
            products_page(
                [{"id": f"gid://shopify/Product/{i}", "status": "DRAFT"} for i in range(2)],
                has_next=True,
                cursor="C1",
            )
        ]
    )
    nodes, capped = ops.read_products(fc, status="DRAFT", limit=2)
    assert len(nodes) == 2
    assert capped is True
    assert fc.calls[0][1]["query"] == "status:DRAFT"
    assert fc.calls[0][1]["first"] == 2


def test_read_product_by_id_coerces_gid_and_paginates():
    product = {"id": "gid://shopify/Product/7", "title": "T", "handle": "h", "status": "ACTIVE"}
    fc = FakeClient(
        [{"product": dict(product, variants={"nodes": [], "pageInfo": {"hasNextPage": False}})}]
    )
    got, variants, capped = ops.read_product(fc, product_id="7")
    assert got["id"] == "gid://shopify/Product/7"
    assert variants == []
    assert capped is False
    # gid coercion happens inside the operation
    assert fc.calls[0][1]["id"] == "gid://shopify/Product/7"
    assert fc.calls[0][0] == q.GET_PRODUCT_BY_ID


def test_read_product_by_handle_uses_handle_query():
    fc = FakeClient(
        [
            {
                "productByHandle": {
                    "id": "x",
                    "variants": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                }
            }
        ]
    )
    got, _variants, _capped = ops.read_product(fc, handle="cool-thing")
    assert got["id"] == "x"
    assert fc.calls[0][0] == q.GET_PRODUCT_BY_HANDLE
    assert fc.calls[0][1]["handle"] == "cool-thing"


def test_read_product_full_by_id():
    fc = FakeClient(
        [{"product": {"id": "g", "variants": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}]
    )
    got, _v, _c = ops.read_product_full(fc, product_id="9")
    assert got["id"] == "g"
    assert fc.calls[0][0] == q.GET_PRODUCT_FULL_BY_ID


def test_read_product_full_by_handle():
    fc = FakeClient(
        [
            {
                "productByHandle": {
                    "id": "g",
                    "variants": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                }
            }
        ]
    )
    got, _v, _c = ops.read_product_full(fc, handle="h")
    assert got["id"] == "g"
    assert fc.calls[0][0] == q.GET_PRODUCT_FULL_BY_HANDLE


def test_read_product_description_by_id_and_handle():
    fc = FakeClient([{"product": {"id": "g", "bodyHtml": "<p>x</p>"}}])
    assert ops.read_product_description(fc, product_id="3")["bodyHtml"] == "<p>x</p>"
    assert fc.calls[0][0] == q.GET_PRODUCT_BY_ID
    fc2 = FakeClient([{"productByHandle": {"id": "g"}}])
    assert ops.read_product_description(fc2, handle="h")["id"] == "g"
    assert fc2.calls[0][0] == q.GET_PRODUCT_BY_HANDLE


def test_read_product_seo():
    fc = FakeClient([{"product": {"id": "g", "seo": {"title": "S"}}}])
    assert ops.read_product_seo(fc, "5")["seo"]["title"] == "S"
    assert fc.calls[0][0] == q.GET_PRODUCT_SEO_BY_ID
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/5"}


def test_read_product_collections():
    fc = FakeClient([{"product": {"id": "g", "collections": {"nodes": []}}}])
    assert ops.read_product_collections(fc, "5")["id"] == "g"
    assert fc.calls[0][0] == q.GET_PRODUCT_COLLECTIONS


# ---------- Story 10.76: the three sibling outer-connection reads ----------
#
# Query-shape pins first. FakeClient never parses the GraphQL and the transport
# is built with fetch_schema_from_transport=False, so reverting any one of these
# queries to its pre-10.76 single-shot text would leave every behavioural test
# below green while the conversion was silently undone. Story 10.72 shipped
# exactly that gap at the query layer; this is the guard its review added,
# parametrised per query so a revert names the query it broke.


def _selection_at(query_text: str, path: list[str]) -> graphql.SelectionSetNode:
    """Walk a parsed query down `path` and return that field's selection set.

    Mirrors what client.paginate() does at runtime with connection_path, so a
    query whose pageInfo sits at the wrong nesting level fails here the way it
    would fail against the real API.

    Every step of the path must be UNALIASED (Story 10.84). paginate() descends
    the RESPONSE by key, and the GraphQL response key for a field is its alias
    when one is present and its name otherwise — so name-matching alone is only
    a mirror of the runtime while no field on the path carries an alias. Adding
    one is valid GraphQL that Shopify answers happily: the AST still reports the
    name, every downstream assertion here still holds, and paginate() reads {}
    and returns zero nodes with capped=False. Asserting it here covers every
    step of every path any caller walks, rather than one field at one call site.
    """
    node: Any = graphql.parse(query_text).definitions[0]
    for key in path:
        matches = [
            sel
            for sel in node.selection_set.selections
            if isinstance(sel, graphql.FieldNode) and sel.name.value == key
        ]
        assert matches, f"no field {key!r} in selection set"
        node = matches[0]
        assert node.alias is None, (
            f"{key!r} is aliased to {node.alias.value!r}; paginate() reads the "
            f"response key, which would be {node.alias.value!r}"
        )
    return node.selection_set


def test_selection_at_rejects_an_aliased_field():
    """AC4 (Story 10.84): the walker refuses an alias on any step of the path.

    Pinned against a literal query rather than a production one, so the guard
    keeps its teeth even if every query in shopify/queries/ is rewritten. An
    alias is valid GraphQL that Shopify answers happily, and the response then
    carries the ALIAS as its key while the AST still reports the name — so a
    name-matching walker stays satisfied while paginate() reads {} and returns
    zero nodes with capped=False, indistinguishable from an empty collection.
    """
    aliased = """
    query Q($first: Int!, $after: String) {
      x: products(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    with pytest.raises(AssertionError, match="aliased"):
        _selection_at(aliased, ["products"])


@pytest.mark.parametrize(
    ("name", "query_text", "connection_path"),
    [
        ("GET_PRODUCTS_BY_COLLECTION", q.GET_PRODUCTS_BY_COLLECTION, ["collectionByHandle"]),
        ("GET_PRODUCTS_WITH_DESCRIPTIONS", q.GET_PRODUCTS_WITH_DESCRIPTIONS, []),
        (
            "GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS",
            q.GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS,
            ["collectionByHandle"],
        ),
    ],
)
def test_sibling_read_queries_have_the_shape_paginate_requires(name, query_text, connection_path):
    """AC2: each of the three carries what client.paginate() documents as its
    requirement — $after, after: $after, and pageInfo { hasNextPage endCursor }.

    Parsed, not substring-matched. A substring check passes as long as the
    tokens appear SOMEWHERE, so it cannot tell a converted query from a
    mis-converted one: hoisting pageInfo out of the products connection up onto
    collectionByHandle keeps every token present, yet paginate() would then read
    pageInfo as {} and stop after one page — and the query would be a schema
    error live. The review that found this had the whole suite green under
    exactly that mutation. graphql-core is pinned in both lockfiles (it is gql's
    own dependency), so parsing costs no new dependency.
    """
    products_selection = _selection_at(query_text, [*connection_path, "products"])
    fields = {
        sel.name.value
        for sel in products_selection.selections
        if isinstance(sel, graphql.FieldNode)
    }
    assert "pageInfo" in fields, f"{name}: pageInfo is not on the walked products connection"

    page_info = _selection_at(query_text, [*connection_path, "products", "pageInfo"])
    page_info_fields = {
        sel.name.value for sel in page_info.selections if isinstance(sel, graphql.FieldNode)
    }
    assert {"hasNextPage", "endCursor"} <= page_info_fields, f"{name}: {page_info_fields}"

    products_field = next(
        sel
        for sel in _selection_at(query_text, connection_path).selections
        if isinstance(sel, graphql.FieldNode) and sel.name.value == "products"
    )
    args = {a.name.value: a.value for a in products_field.arguments}
    assert "after" in args, f"{name}: products(...) takes no after argument"
    assert isinstance(args["after"], graphql.VariableNode)
    assert args["after"].name.value == "after", f"{name}: after is not bound to $after"

    declared = {
        v.variable.name.value for v in graphql.parse(query_text).definitions[0].variable_definitions
    }
    assert "after" in declared, f"{name} does not declare $after"


def test_read_products_by_collection():
    fc = FakeClient([collection_products_page([], collection_id="c")])
    col, nodes, capped = ops.read_products_by_collection(fc, "vanish")
    assert col["id"] == "c"
    assert nodes == []
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCTS_BY_COLLECTION
    assert fc.calls[0][1] == {"handle": "vanish", "first": 250, "after": None}


def test_read_products_by_collection_follows_cursor_across_pages():
    """AC3: a first page reporting hasNextPage=True yields the SECOND page's
    products too, and the second request carries the first page's endCursor."""
    fc = FakeClient(
        [
            collection_products_page([{"id": "p1"}], has_next=True, cursor="C1"),
            collection_products_page([{"id": "p2"}]),
        ]
    )
    _col, nodes, capped = ops.read_products_by_collection(fc, "vanish")
    assert [n["id"] for n in nodes] == ["p1", "p2"]
    assert capped is False
    assert fc.calls[1][1]["after"] == "C1"


def test_read_products_by_collection_reports_capped_at_the_page_budget():
    pages = [
        collection_products_page([{"id": f"p{i}"}], has_next=True, cursor=f"C{i}")
        for i in range(ops.PRODUCTS_MAX_PAGES)
    ]
    fc = FakeClient(pages)
    _col, nodes, capped = ops.read_products_by_collection(fc, "vanish")
    assert capped is True
    assert len(fc.calls) == ops.PRODUCTS_MAX_PAGES
    assert len(nodes) == ops.PRODUCTS_MAX_PAGES


def test_read_products_by_collection_missing_handle_yields_none_in_one_request():
    """AC3: a null collectionByHandle must stay distinguishable from an empty
    collection. paginate() walks it to ({"collectionByHandle": None}, [], False),
    so the operation has to inspect the first-page dict, not the node list."""
    fc = FakeClient([{"collectionByHandle": None}])
    col, nodes, capped = ops.read_products_by_collection(fc, "nope")
    assert col is None
    assert nodes == []
    assert capped is False
    assert len(fc.calls) == 1


def test_read_products_by_collection_takes_collection_fields_from_the_first_page():
    """AC4: the collection's own id/title/handle come from the FIRST page. The
    two pages deliberately disagree, so a last-page-wins implementation fails."""
    fc = FakeClient(
        [
            collection_products_page(
                [{"id": "p1"}],
                has_next=True,
                cursor="C1",
                collection_id="FIRST",
                title="First Title",
                handle="first-handle",
            ),
            collection_products_page(
                [{"id": "p2"}],
                collection_id="SECOND",
                title="Second Title",
                handle="second-handle",
            ),
        ]
    )
    col, nodes, _capped = ops.read_products_by_collection(fc, "vanish")
    assert col["id"] == "FIRST"
    assert col["title"] == "First Title"
    assert col["handle"] == "first-handle"
    assert len(nodes) == 2


def test_read_products_with_descriptions_all():
    fc = FakeClient([products_page([{"id": "g"}])])
    nodes, capped = ops.read_products_with_descriptions(fc, limit=10)
    assert nodes == [{"id": "g"}]
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCTS_WITH_DESCRIPTIONS
    assert fc.calls[0][1] == {"first": 10, "after": None}


def test_read_products_with_descriptions_limit_is_a_total_not_a_page_size():
    """AC5: a full page that still reports hasNextPage is capped, in ONE
    request — `limit` bounds the total, and asking for exactly `limit` items
    does not buy a second page just to discover there are more."""
    fc = FakeClient(
        [products_page([{"id": f"p{i}"} for i in range(3)], has_next=True, cursor="C1")]
    )
    nodes, capped = ops.read_products_with_descriptions(fc, limit=3)
    assert len(nodes) == 3
    assert capped is True
    assert len(fc.calls) == 1


def test_read_products_with_descriptions_trims_the_overshoot_and_reports_capped():
    """AC5: a limit that is not a whole number of pages overshoots; the
    discarded remainder is itself proof that more products exist."""
    limit = ops.PRODUCTS_PAGE_SIZE + 1
    fc = FakeClient(
        [
            products_page(
                [{"id": f"a{i}"} for i in range(ops.PRODUCTS_PAGE_SIZE)],
                has_next=True,
                cursor="C1",
            ),
            products_page([{"id": f"b{i}"} for i in range(ops.PRODUCTS_PAGE_SIZE)]),
        ]
    )
    nodes, capped = ops.read_products_with_descriptions(fc, limit=limit)
    assert len(nodes) == limit
    assert capped is True


def test_read_products_with_descriptions_limit_cannot_widen_the_request_budget():
    """AC5: the guard whose absence was Story 10.72's High finding — a huge
    limit derives a huge max_pages unless it is clamped."""
    pages = [
        products_page(
            [{"id": f"p{i}-{j}"} for j in range(ops.PRODUCTS_PAGE_SIZE)],
            has_next=True,
            cursor=f"C{i}",
        )
        for i in range(ops.PRODUCTS_MAX_PAGES)
    ]
    fc = FakeClient(pages)
    nodes, capped = ops.read_products_with_descriptions(fc, limit=10**9)
    assert len(fc.calls) == ops.PRODUCTS_MAX_PAGES, "limit must not buy extra requests"
    assert len(nodes) == ops.PRODUCTS_PAGE_SIZE * ops.PRODUCTS_MAX_PAGES
    assert capped is True


def test_read_collection_with_descriptions():
    fc = FakeClient([collection_products_page([], collection_id="c")])
    col, nodes, capped = ops.read_collection_with_descriptions(fc, "vanish", 25)
    assert col["id"] == "c"
    assert nodes == []
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS
    assert fc.calls[0][1] == {"handle": "vanish", "first": 25, "after": None}


def test_read_collection_with_descriptions_missing_handle_yields_none():
    fc = FakeClient([{"collectionByHandle": None}])
    col, nodes, capped = ops.read_collection_with_descriptions(fc, "nope", 25)
    assert col is None
    assert nodes == []
    assert capped is False
    assert len(fc.calls) == 1


def test_read_collection_with_descriptions_takes_collection_fields_from_the_first_page():
    """AC4 for the SECOND collection read. The first-page rule was pinned for
    read_products_by_collection only, so a last-page-wins mutation here was
    invisible — the verifier demonstrated it with the whole suite green."""
    fc = FakeClient(
        [
            collection_products_page(
                [{"id": "p1"}],
                has_next=True,
                cursor="C1",
                collection_id="FIRST",
                title="First Title",
                handle="first-handle",
            ),
            collection_products_page(
                [{"id": "p2"}],
                collection_id="SECOND",
                title="Second Title",
                handle="second-handle",
            ),
        ]
    )
    col, nodes, _capped = ops.read_collection_with_descriptions(fc, "vanish", 0)
    assert col["id"] == "FIRST"
    assert col["title"] == "First Title"
    assert col["handle"] == "first-handle"
    assert len(nodes) == 2


@pytest.mark.parametrize(
    "op",
    [
        lambda fc: ops.read_products_by_collection(fc, "vanish"),
        lambda fc: ops.read_collection_with_descriptions(fc, "vanish", 0),
    ],
)
def test_collection_reads_do_not_hand_back_a_first_page_only_products_connection(op):
    """The returned collection dict must NOT carry a `products` key.

    It would hold page ONE only, so the pre-10.76 call-site idiom
    `col.get("products", {}).get("nodes", [])` would still compile and silently
    yield a truncated list — the exact silent truncation this story removes.
    Two reviewers found the trap independently; this pins it shut."""
    fc = FakeClient(
        [
            collection_products_page([{"id": "p1"}], has_next=True, cursor="C1"),
            collection_products_page([{"id": "p2"}]),
        ]
    )
    col, nodes, _capped = op(fc)
    assert "products" not in col
    assert [n["id"] for n in nodes] == ["p1", "p2"]


def test_collection_head_does_not_mutate_the_response():
    """Stripping builds a new dict; the caller's response object is untouched."""
    page = collection_products_page([{"id": "p1"}])
    fc = FakeClient([page])
    ops.read_products_by_collection(fc, "vanish")
    assert "products" in page["collectionByHandle"]


def test_read_collection_with_descriptions_limit_is_a_total():
    fc = FakeClient(
        [collection_products_page([{"id": f"p{i}"} for i in range(3)], has_next=True, cursor="C1")]
    )
    _col, nodes, capped = ops.read_collection_with_descriptions(fc, "vanish", 3)
    assert len(nodes) == 3
    assert capped is True
    assert len(fc.calls) == 1


def test_read_collection_with_descriptions_limit_cannot_widen_the_request_budget():
    pages = [
        collection_products_page(
            [{"id": f"p{i}-{j}"} for j in range(ops.PRODUCTS_PAGE_SIZE)],
            has_next=True,
            cursor=f"C{i}",
        )
        for i in range(ops.PRODUCTS_MAX_PAGES)
    ]
    fc = FakeClient(pages)
    _col, nodes, capped = ops.read_collection_with_descriptions(fc, "vanish", 10**9)
    assert len(fc.calls) == ops.PRODUCTS_MAX_PAGES, "limit must not buy extra requests"
    assert len(nodes) == ops.PRODUCTS_PAGE_SIZE * ops.PRODUCTS_MAX_PAGES
    assert capped is True


def test_read_product_variants_policy():
    fc = FakeClient(
        [
            {
                "product": {
                    "id": "g",
                    "title": "T",
                    "variants": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                }
            }
        ]
    )
    product, variants, capped = ops.read_product_variants_policy(fc, "5")
    assert product["title"] == "T"
    assert variants == []
    assert capped is False
    assert fc.calls[0][0] == q.GET_PRODUCT_VARIANTS_POLICY


def test_fetch_product_core_for_preview():
    fc = FakeClient([{"product": {"id": "g", "title": "T", "handle": "h"}}])
    assert ops.fetch_product_core(fc, "5")["title"] == "T"
    assert fc.calls[0][0] == q.GET_PRODUCT_BY_ID
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/5"}


def test_fetch_product_full_record_for_preview():
    fc = FakeClient([{"product": {"id": "g", "tags": ["a"]}}])
    assert ops.fetch_product_full_record(fc, "5")["tags"] == ["a"]
    assert fc.calls[0][0] == q.GET_PRODUCT_FULL_BY_ID


# ---------- write operations (return raw mutation result) ----------


def test_update_product_title_builds_input_and_returns_result():
    fc = FakeClient([{"productUpdate": {"product": {"id": "g"}, "userErrors": []}}])
    result = ops.update_product_title(fc, "5", "New", "new-handle")
    assert result["productUpdate"]["product"]["id"] == "g"
    assert fc.calls[0][0] == q.UPDATE_PRODUCT
    assert fc.calls[0][1]["input"] == {
        "id": "gid://shopify/Product/5",
        "title": "New",
        "handle": "new-handle",
    }


def test_update_product_description_builds_input():
    fc = FakeClient([{"productUpdate": {"userErrors": []}}])
    ops.update_product_description(fc, "5", "<p>new</p>")
    assert fc.calls[0][0] == q.UPDATE_PRODUCT
    assert fc.calls[0][1]["input"] == {
        "id": "gid://shopify/Product/5",
        "descriptionHtml": "<p>new</p>",
    }


def test_update_product_seo_builds_input():
    fc = FakeClient([{"productUpdate": {"userErrors": []}}])
    ops.update_product_seo(fc, "5", {"title": "S"})
    assert fc.calls[0][1]["input"] == {"id": "gid://shopify/Product/5", "seo": {"title": "S"}}


def test_update_product_tags_builds_input():
    fc = FakeClient([{"productUpdate": {"userErrors": []}}])
    ops.update_product_tags(fc, "5", ["a", "b"])
    assert fc.calls[0][0] == q.UPDATE_PRODUCT_TAGS
    assert fc.calls[0][1]["input"] == {"id": "gid://shopify/Product/5", "tags": ["a", "b"]}


def test_update_product_status_builds_input():
    fc = FakeClient([{"productUpdate": {"userErrors": []}}])
    ops.update_product_status(fc, "5", "ARCHIVED")
    assert fc.calls[0][0] == q.UPDATE_PRODUCT_STATUS
    assert fc.calls[0][1]["input"] == {"id": "gid://shopify/Product/5", "status": "ARCHIVED"}


def test_update_variant_inventory_policy_builds_input():
    fc = FakeClient([{"productVariantsBulkUpdate": {"productVariants": [], "userErrors": []}}])
    ops.update_variant_inventory_policy(
        fc, "5", [{"id": "gid://shopify/ProductVariant/1", "inventoryPolicy": "DENY"}]
    )
    assert fc.calls[0][0] == q.UPDATE_PRODUCT_VARIANTS_POLICY
    assert fc.calls[0][1] == {
        "productId": "gid://shopify/Product/5",
        "variants": [{"id": "gid://shopify/ProductVariant/1", "inventoryPolicy": "DENY"}],
    }


# ---------- Story 10.68 (T-10.65-refuse-both-fanout): both-supplied is refused ----------

# The two identifiers deliberately name *different* products: `_S1068_DECOY_ID`
# is the legacy id of one product, `_S1068_HANDLE` the handle of another. The
# failure this guards is "the wrong one was silently chosen", so a fixture where
# both point at the same product could not tell the two outcomes apart.
_S1068_DECOY_ID = "111"
_S1068_HANDLE = "some-other-product"
_S1068_DECOY_GID = f"gid://shopify/Product/{_S1068_DECOY_ID}"

_S1068_READS = [ops.read_product, ops.read_product_full, ops.read_product_description]


@pytest.mark.parametrize("fn", _S1068_READS, ids=lambda f: f.__name__)
def test_s1068_both_identifiers_refused_before_any_network(fn):
    """Story 10.68 extends Story 10.65's rule past catalog_hygiene: supplying
    both identifiers is ambiguous, so it is rejected here rather than silently
    resolved by `product_id` with the handle discarded."""
    fc = FakeClient([])
    with pytest.raises(ValueError, match="not both"):
        fn(fc, product_id=_S1068_DECOY_ID, handle=_S1068_HANDLE)
    assert fc.calls == []


@pytest.mark.parametrize("fn", _S1068_READS, ids=lambda f: f.__name__)
def test_s1068_both_supplied_never_reads_the_product_id_twin(fn):
    """The specific outcome the old precedence produced: the `product_id`
    product was read and the handle silently dropped. Prove that GID is never
    addressed at all."""
    fc = FakeClient([])
    with pytest.raises(ValueError):
        fn(fc, product_id=_S1068_DECOY_ID, handle=_S1068_HANDLE)
    assert _S1068_DECOY_GID not in str(fc.calls)


@pytest.mark.parametrize("fn", _S1068_READS, ids=lambda f: f.__name__)
def test_s1068_neither_identifier_still_raises_its_original_message(fn):
    """Regression guard: the both-supplied rejection must not disturb the
    neither-supplied guard `_require_discriminator` already provided."""
    fc = FakeClient([])
    with pytest.raises(ValueError, match="provide either product_id or handle"):
        fn(fc, product_id="", handle="")
    assert fc.calls == []
