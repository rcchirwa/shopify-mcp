"""
Offline unit tests for shopify.operations.catalog_hygiene.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the catalog_hygiene operations are callable from
non-MCP entry points (Story 10.25 / A5, AC4). They also pin the shared-fragment
reuse across the by-id / by-handle query pairs (AC3) and cover the dynamic
GraphQL query builders that live in shopify.queries.catalog_hygiene.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_catalog_hygiene_operations_offline.py -v
"""

import pytest

from _testing import FakeClient
from shopify.operations import catalog_hygiene as ops
from shopify.queries import catalog_hygiene as q

# ---------- AC3: shared GraphQL fragments reused across by-id / by-handle -----


def test_vendor_fragment_defined_once_and_reused():
    """GET_PRODUCT_VENDOR and GET_PRODUCT_VENDOR_BY_HANDLE both spread the same
    vendor fragment instead of duplicating the selection set."""
    assert "fragment ProductVendorFields on Product" in q.GET_PRODUCT_VENDOR
    assert "fragment ProductVendorFields on Product" in q.GET_PRODUCT_VENDOR_BY_HANDLE
    assert "...ProductVendorFields" in q.GET_PRODUCT_VENDOR
    assert "...ProductVendorFields" in q.GET_PRODUCT_VENDOR_BY_HANDLE


def test_type_fragment_defined_once_and_reused():
    assert "fragment ProductTypeFields on Product" in q.GET_PRODUCT_TYPE
    assert "fragment ProductTypeFields on Product" in q.GET_PRODUCT_TYPE_BY_HANDLE
    assert "...ProductTypeFields" in q.GET_PRODUCT_TYPE
    assert "...ProductTypeFields" in q.GET_PRODUCT_TYPE_BY_HANDLE


def test_options_fragment_defined_once_and_reused():
    assert "fragment ProductOptionsFields on Product" in q.GET_PRODUCT_OPTIONS
    assert "fragment ProductOptionsFields on Product" in q.GET_PRODUCT_OPTIONS_BY_HANDLE
    assert "...ProductOptionsFields" in q.GET_PRODUCT_OPTIONS
    assert "...ProductOptionsFields" in q.GET_PRODUCT_OPTIONS_BY_HANDLE


# ---------- read operations (build vars + execute, return raw result) --------


def test_read_variants_for_pricing():
    fc = FakeClient([{"product": {"id": "g", "title": "T", "variants": {"nodes": []}}}])
    out = ops.read_variants_for_pricing(fc, "gid://shopify/Product/5")
    assert out["product"]["title"] == "T"
    assert fc.calls[0][0] == q.GET_PRODUCT_VARIANTS_FOR_PRICING
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/5"}


def test_read_product_category():
    fc = FakeClient([{"product": {"id": "g", "category": {"id": "c"}}}])
    out = ops.read_product_category(fc, "gid://shopify/Product/5")
    assert out["product"]["category"]["id"] == "c"
    assert fc.calls[0][0] == q.GET_PRODUCT_CATEGORY
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/5"}


def test_read_product_by_handle_min():
    fc = FakeClient([{"productByHandle": {"id": "gid://shopify/Product/9"}}])
    out = ops.read_product_by_handle_min(fc, "cool-tee")
    assert out["productByHandle"]["id"] == "gid://shopify/Product/9"
    assert fc.calls[0][0] == q.GET_PRODUCT_BY_HANDLE_MIN
    assert fc.calls[0][1] == {"handle": "cool-tee"}


def test_search_taxonomy_categories():
    fc = FakeClient([{"taxonomy": {"categories": {"nodes": [{"id": "t"}]}}}])
    out = ops.search_taxonomy_categories(fc, "shirts")
    assert out["taxonomy"]["categories"]["nodes"] == [{"id": "t"}]
    assert fc.calls[0][0] == q.TAXONOMY_SEARCH
    assert fc.calls[0][1] == {"search": "shirts"}


def test_read_product_snapshot_by_id_uses_passed_query():
    fc = FakeClient([{"product": {"id": "g", "vendor": "Nike"}}])
    out = ops.read_product_snapshot_by_id(fc, q.GET_PRODUCT_VENDOR, "gid://shopify/Product/5")
    assert out["product"]["vendor"] == "Nike"
    assert fc.calls[0][0] == q.GET_PRODUCT_VENDOR
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/5"}


def test_read_product_snapshot_by_handle_uses_passed_query():
    fc = FakeClient([{"productByHandle": {"id": "g", "productType": "Tee"}}])
    out = ops.read_product_snapshot_by_handle(fc, q.GET_PRODUCT_TYPE_BY_HANDLE, "h")
    assert out["productByHandle"]["productType"] == "Tee"
    assert fc.calls[0][0] == q.GET_PRODUCT_TYPE_BY_HANDLE
    assert fc.calls[0][1] == {"handle": "h"}


def test_read_product_media_and_variant_media():
    fc = FakeClient([{"product": {"id": "g", "media": {"nodes": []}}}])
    out = ops.read_product_media_and_variant_media(
        fc, "gid://shopify/Product/5", media_first=50, media_after=None
    )
    assert out["product"]["id"] == "g"
    assert fc.calls[0][0] == q.GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA
    assert fc.calls[0][1] == {
        "id": "gid://shopify/Product/5",
        "mediaFirst": 50,
        "mediaAfter": None,
    }


def test_read_product_media_page():
    fc = FakeClient([{"product": {"id": "g", "media": {"nodes": []}}}])
    out = ops.read_product_media_page(
        fc, "gid://shopify/Product/5", media_first=50, media_after="cursor-x"
    )
    assert out["product"]["id"] == "g"
    assert fc.calls[0][0] == q.GET_PRODUCT_MEDIA_PAGE
    assert fc.calls[0][1] == {
        "id": "gid://shopify/Product/5",
        "mediaFirst": 50,
        "mediaAfter": "cursor-x",
    }


def test_read_product_metafields_page_keys_mode_with_variants():
    fc = FakeClient([{"product": {"id": "g"}}])
    query = q._build_get_product_and_variant_metafields_query("keys")
    ops.read_product_metafields_page(
        fc,
        query=query,
        product_gid="gid://shopify/Product/5",
        page_size=100,
        filter_mode="keys",
        ns_filter=None,
        keys_filter=["google.age_group"],
        fetch_metafields=True,
        metafields_cursor="CUR",
        fetch_variants=True,
        variants_page_size=50,
        variants_cursor="VCUR",
    )
    assert fc.calls[0][0] == query
    assert fc.calls[0][1] == {
        "id": "gid://shopify/Product/5",
        "first": 100,
        "keys": ["google.age_group"],
        "after": "CUR",
        "variantsFirst": 50,
        "variantsAfter": "VCUR",
    }


def test_read_product_metafields_page_namespace_mode_metafields_only():
    fc = FakeClient([{"product": {"id": "g"}}])
    query = q._build_get_product_metafields_query("namespace")
    ops.read_product_metafields_page(
        fc,
        query=query,
        product_gid="gid://shopify/Product/5",
        page_size=100,
        filter_mode="namespace",
        ns_filter="custom",
        keys_filter=None,
        fetch_metafields=True,
        metafields_cursor=None,
        fetch_variants=False,
        variants_page_size=50,
        variants_cursor=None,
    )
    assert fc.calls[0][1] == {
        "id": "gid://shopify/Product/5",
        "first": 100,
        "namespace": "custom",
        "after": None,
    }


def test_read_product_metafields_page_none_mode_variants_only():
    fc = FakeClient([{"product": {"id": "g"}}])
    query = q._build_get_product_variant_metafields_page_query("none")
    ops.read_product_metafields_page(
        fc,
        query=query,
        product_gid="gid://shopify/Product/5",
        page_size=100,
        filter_mode="none",
        ns_filter=None,
        keys_filter=None,
        fetch_metafields=False,
        metafields_cursor=None,
        fetch_variants=True,
        variants_page_size=50,
        variants_cursor="VCUR",
    )
    assert fc.calls[0][1] == {
        "id": "gid://shopify/Product/5",
        "first": 100,
        "variantsFirst": 50,
        "variantsAfter": "VCUR",
    }


def test_resolve_metafields_batch_builds_and_executes():
    classified = [
        {"idx": 0, "mode": "gid", "gid": "gid://shopify/Metafield/1"},
        {
            "idx": 1,
            "mode": "triple",
            "ownerId": "gid://shopify/Product/5",
            "ownerType": "PRODUCT",
            "namespace": "custom",
            "key": "fabric",
        },
    ]
    fc = FakeClient([{"e0": {"id": "gid://shopify/Metafield/1"}, "e1": {"metafield": None}}])
    out = ops.resolve_metafields_batch(fc, classified)
    assert out["e0"]["id"] == "gid://shopify/Metafield/1"
    assert "BatchResolveMetafields" in fc.calls[0][0]
    assert fc.calls[0][1]["id0"] == "gid://shopify/Metafield/1"
    assert fc.calls[0][1]["ownerId1"] == "gid://shopify/Product/5"


# ---------- write operations (build input + execute, return raw result) ------


def test_update_variants_pricing():
    fc = FakeClient([{"productVariantsBulkUpdate": {"productVariants": [], "userErrors": []}}])
    variants_input = [{"id": "gid://shopify/ProductVariant/1", "price": "9.99"}]
    ops.update_variants_pricing(fc, "gid://shopify/Product/5", variants_input)
    assert fc.calls[0][0] == q.UPDATE_PRODUCT_VARIANTS_PRICING
    assert fc.calls[0][1] == {
        "productId": "gid://shopify/Product/5",
        "variants": variants_input,
    }


def test_update_product_category():
    fc = FakeClient([{"productUpdate": {"product": {"id": "g"}, "userErrors": []}}])
    ops.update_product_category(fc, "gid://shopify/Product/5", "gid://shopify/TaxonomyCategory/7")
    assert fc.calls[0][0] == q.UPDATE_PRODUCT_CATEGORY
    assert fc.calls[0][1] == {
        "product": {
            "id": "gid://shopify/Product/5",
            "category": "gid://shopify/TaxonomyCategory/7",
        }
    }


def test_update_product_vendor():
    fc = FakeClient([{"productUpdate": {"product": {"id": "g", "vendor": "V"}, "userErrors": []}}])
    ops.update_product_vendor(fc, "gid://shopify/Product/5", "Vanish")
    assert fc.calls[0][0] == q.UPDATE_PRODUCT_VENDOR
    assert fc.calls[0][1] == {"product": {"id": "gid://shopify/Product/5", "vendor": "Vanish"}}


def test_update_product_vendor_clear_sends_none():
    fc = FakeClient([{"productUpdate": {"product": {"id": "g", "vendor": None}, "userErrors": []}}])
    ops.update_product_vendor(fc, "gid://shopify/Product/5", None)
    assert fc.calls[0][1] == {"product": {"id": "gid://shopify/Product/5", "vendor": None}}


def test_update_product_type():
    fc = FakeClient(
        [{"productUpdate": {"product": {"id": "g", "productType": "T"}, "userErrors": []}}]
    )
    ops.update_product_type(fc, "gid://shopify/Product/5", "Hoodie")
    assert fc.calls[0][0] == q.UPDATE_PRODUCT_TYPE
    assert fc.calls[0][1] == {"product": {"id": "gid://shopify/Product/5", "productType": "Hoodie"}}


def test_detach_variant_media():
    fc = FakeClient([{"productVariantDetachMedia": {"product": {"id": "g"}, "userErrors": []}}])
    entries = [
        {"variantId": "gid://shopify/ProductVariant/1", "mediaIds": ["gid://shopify/MediaImage/1"]}
    ]
    ops.detach_variant_media(fc, "gid://shopify/Product/5", entries)
    assert fc.calls[0][0] == q.PRODUCT_VARIANT_DETACH_MEDIA
    assert fc.calls[0][1] == {"productId": "gid://shopify/Product/5", "variantMedia": entries}


def test_append_variant_media():
    fc = FakeClient([{"productVariantAppendMedia": {"productVariants": [], "userErrors": []}}])
    entries = [
        {"variantId": "gid://shopify/ProductVariant/1", "mediaIds": ["gid://shopify/MediaImage/1"]}
    ]
    ops.append_variant_media(fc, "gid://shopify/Product/5", entries)
    assert fc.calls[0][0] == q.PRODUCT_VARIANT_APPEND_MEDIA
    assert fc.calls[0][1] == {"productId": "gid://shopify/Product/5", "variantMedia": entries}


def test_set_metafields():
    fc = FakeClient([{"metafieldsSet": {"metafields": [], "userErrors": []}}])
    rows = [
        {
            "ownerId": "gid://shopify/Product/5",
            "namespace": "custom",
            "key": "k",
            "value": "v",
            "type": "single_line_text_field",
        }
    ]
    ops.set_metafields(fc, rows)
    assert fc.calls[0][0] == q.METAFIELDS_SET_MUTATION
    assert fc.calls[0][1] == {"metafields": rows}


def test_delete_metafields():
    fc = FakeClient([{"metafieldsDelete": {"deletedMetafields": [], "userErrors": []}}])
    rows = [{"ownerId": "gid://shopify/Product/5", "namespace": "custom", "key": "k"}]
    ops.delete_metafields(fc, rows)
    assert fc.calls[0][0] == q.METAFIELDS_DELETE_MUTATION
    assert fc.calls[0][1] == {"metafields": rows}


def test_update_product_option():
    fc = FakeClient([{"productOptionUpdate": {"product": {"id": "g"}, "userErrors": []}}])
    option_input = {"id": "gid://shopify/ProductOption/1", "name": "Size"}
    values = [{"id": "gid://shopify/ProductOptionValue/2", "name": "Large"}]
    ops.update_product_option(fc, "gid://shopify/Product/5", option_input, values, "LEAVE_AS_IS")
    assert fc.calls[0][0] == q.UPDATE_PRODUCT_OPTION
    assert fc.calls[0][1] == {
        "productId": "gid://shopify/Product/5",
        "option": option_input,
        "optionValuesToUpdate": values,
        "variantStrategy": "LEAVE_AS_IS",
    }


# ---------- query builders (live in shopify.queries.catalog_hygiene) ---------


def test_builders_reject_unknown_filter_mode():
    for builder in (
        q._build_get_product_metafields_query,
        q._build_get_product_and_variant_metafields_query,
        q._build_get_product_variant_metafields_page_query,
    ):
        with pytest.raises(ValueError, match="unknown metafield filter mode"):
            builder("invalid_mode")


@pytest.mark.parametrize("mode", ["keys", "namespace", "none"])
def test_build_product_metafields_query_modes(mode):
    query = q._build_get_product_metafields_query(mode)
    assert "GetProductMetafields" in query


def test_build_batch_resolve_query_mixed_modes():
    classified = [
        {"mode": "gid", "gid": "gid://shopify/Metafield/1"},
        {
            "mode": "triple",
            "ownerId": "gid://shopify/Product/5",
            "namespace": "custom",
            "key": "fabric",
        },
    ]
    query, variables = q._build_batch_resolve_query(classified)
    assert "BatchResolveMetafields" in query
    assert variables == {
        "id0": "gid://shopify/Metafield/1",
        "ownerId1": "gid://shopify/Product/5",
        "ns1": "custom",
        "k1": "fabric",
    }
