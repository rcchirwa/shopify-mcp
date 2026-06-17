"""
Offline unit tests for shopify.operations.products.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the operations are callable from non-MCP entry
points (Story 10.23 / A5, AC4). They also pin the shared-fragment reuse across
the by-id and by-handle product queries (AC3).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_products_operations_offline.py -v
"""

from _testing import FakeClient
from shopify.operations import products as ops
from shopify.queries import products as q

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


# ---------- read operations ----------


def test_read_products_returns_nodes():
    fc = FakeClient([{"products": {"nodes": [{"id": "gid://shopify/Product/1"}]}}])
    nodes = ops.read_products(fc)
    assert nodes == [{"id": "gid://shopify/Product/1"}]
    assert fc.calls[0][0] == q.GET_PRODUCTS
    assert fc.calls[0][1] == {"first": 250}


def test_read_products_empty():
    fc = FakeClient([{}])
    assert ops.read_products(fc) == []


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
