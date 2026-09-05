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

import pytest

from shopify_mcp.shopify.operations import products as ops
from shopify_mcp.shopify.queries import products as q
from tests.support import FakeClient, products_page

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
    # query=None (no status filter) plus the cursor pair paginate() supplies.
    assert fc.calls[0][1] == {"query": None, "first": ops.PRODUCTS_PAGE_SIZE, "after": None}


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


def test_read_products_by_collection():
    fc = FakeClient([{"collectionByHandle": {"id": "c", "products": {"nodes": []}}}])
    assert ops.read_products_by_collection(fc, "vanish")["id"] == "c"
    assert fc.calls[0][0] == q.GET_PRODUCTS_BY_COLLECTION
    assert fc.calls[0][1] == {"handle": "vanish", "first": 250}


def test_read_products_with_descriptions_all():
    fc = FakeClient([{"products": {"nodes": [{"id": "g"}]}}])
    assert ops.read_products_with_descriptions(fc, limit=10) == [{"id": "g"}]
    assert fc.calls[0][0] == q.GET_PRODUCTS_WITH_DESCRIPTIONS
    assert fc.calls[0][1] == {"first": 10}


def test_read_collection_with_descriptions():
    fc = FakeClient([{"collectionByHandle": {"id": "c", "products": {"nodes": []}}}])
    assert ops.read_collection_with_descriptions(fc, "vanish", 25)["id"] == "c"
    assert fc.calls[0][0] == q.GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS
    assert fc.calls[0][1] == {"handle": "vanish", "first": 25}


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
