"""
Offline unit tests for tools/products.py.

Uses a fake client to exercise read-path response unwrap and write-path
preview/mutation-shape branches without hitting Shopify.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_products_offline.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from tools import products
from tools.products import (
    GET_PRODUCT_BY_ID,
    GET_PRODUCT_BY_HANDLE,
    GET_PRODUCT_COLLECTIONS,
    GET_PRODUCT_FULL_BY_ID,
    GET_PRODUCT_FULL_BY_HANDLE,
    GET_PRODUCT_SEO_BY_ID,
    GET_PRODUCT_VARIANTS_POLICY,
    UPDATE_PRODUCT,
    UPDATE_PRODUCT_STATUS,
    UPDATE_PRODUCT_TAGS,
    UPDATE_PRODUCT_VARIANTS_POLICY,
    GET_PRODUCTS,
    GET_PRODUCTS_BY_COLLECTION,
    GET_PRODUCTS_WITH_DESCRIPTIONS,
    GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS,
)


class CapturingServer:
    """Stand-in for FastMCP that records decorated tool functions."""
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    """Scripted responses for client.execute()."""
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
    products.register(srv, fc)
    return srv.tools, fc


def _seo_read(seo_title=None, seo_description=None, pid="123"):
    return {"product": {
        "id": f"gid://shopify/Product/{pid}",
        "title": "T",
        "seo": {"title": seo_title, "description": seo_description},
    }}


def _product_read(pid, title, handle):
    return {"product": {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "handle": handle,
        "status": "ACTIVE",
        "bodyHtml": "",
        "variants": {"nodes": []},
    }}


def _update_ok(pid="123", title="x", handle="x"):
    return {"productUpdate": {
        "product": {"id": f"gid://shopify/Product/{pid}", "title": title, "handle": handle},
        "userErrors": [],
    }}


def _update_err(field, message):
    return {"productUpdate": {"product": None, "userErrors": [{"field": field, "message": message}]}}


# ---------- slugify_shopify_handle ----------

def test_slugify_strips_quotes_and_collapses_dashes():
    cases = [
        ('Iconic "V" Logo Pull Over Hoodie', 'iconic-v-logo-pull-over-hoodie'),
        ("Iconic 'V' Logo", 'iconic-v-logo'),
        ("Iconic \u201cV\u201d Logo", 'iconic-v-logo'),
        ("Iconic \u2018V\u2019 Logo", 'iconic-v-logo'),
        ('Vanish | Iconic V Trucker Hat \u2013 Embroidered Front Logo',
         'vanish-iconic-v-trucker-hat-embroidered-front-logo'),
        ('  leading and trailing  ', 'leading-and-trailing'),
        ('multi   spaces', 'multi-spaces'),
        ('keeps_underscore-and-dash', 'keeps_underscore-and-dash'),
    ]
    for title, expected in cases:
        actual = products.slugify_shopify_handle(title)
        assert actual == expected, f"{title!r} -> {actual!r} (expected {expected!r})"


# ---------- update_product_seo ----------

def test_seo_empty_payload_rejected_no_shopify_call():
    tools, fc = _build([])
    out = tools["update_product_seo"](product_id="123", confirm=True)
    assert out.startswith("Error:"), out
    assert fc.calls == [], "empty payload must not call Shopify"


def test_seo_long_title_warning_preview_only():
    tools, fc = _build([_seo_read()])
    out = tools["update_product_seo"](
        product_id="123", new_seo_title="A" * 80, confirm=False,
    )
    assert "Warnings" in out and "> 70" in out, out
    assert "confirm=True" in out
    assert len(fc.calls) == 1, "only the SEO read should happen in preview mode"
    assert fc.calls[0][0] == GET_PRODUCT_SEO_BY_ID


def test_seo_long_description_warning_preview_only():
    tools, fc = _build([_seo_read()])
    out = tools["update_product_seo"](
        product_id="123", new_seo_description="D" * 200, confirm=False,
    )
    assert "Warnings" in out and "> 160" in out, out


def test_seo_both_fields_mutation_shape():
    tools, fc = _build([_seo_read(), _update_ok(pid="6803111739545")])
    out = tools["update_product_seo"](
        product_id="6803111739545",
        new_seo_title="Vanish Trucker Hat | Streetwear",
        new_seo_description="The signature V, embroidered front and center.",
        confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert "PREVIEW" not in out, out
    _, vars_put = fc.calls[1]
    assert vars_put["input"]["id"] == "gid://shopify/Product/6803111739545"
    assert vars_put["input"]["seo"] == {
        "title": "Vanish Trucker Hat | Streetwear",
        "description": "The signature V, embroidered front and center.",
    }, vars_put
    assert fc.calls[0][0] == GET_PRODUCT_SEO_BY_ID
    assert fc.calls[1][0] == UPDATE_PRODUCT


def test_seo_preview_path_does_not_call_mutation():
    """confirm=False MUST NOT issue UPDATE_PRODUCT (FakeClient has no second response)."""
    tools, fc = _build([_seo_read()])
    out = tools["update_product_seo"](
        product_id="123",
        new_seo_title="Some title",
        new_seo_description="Some description",
        confirm=False,
    )
    assert out.startswith("PREVIEW —"), out
    assert "confirm=True" in out
    assert len(fc.calls) == 1, "preview must not issue the mutation"
    assert fc.calls[0][0] == GET_PRODUCT_SEO_BY_ID


def test_seo_title_only_mutation_shape():
    tools, fc = _build([_seo_read(), _update_ok()])
    tools["update_product_seo"](
        product_id="123", new_seo_title="Only Title", confirm=True,
    )
    _, vars_put = fc.calls[1]
    assert vars_put["input"]["seo"] == {"title": "Only Title"}, vars_put


def test_seo_description_only_mutation_shape():
    tools, fc = _build([_seo_read(), _update_ok()])
    tools["update_product_seo"](
        product_id="123", new_seo_description="Only desc", confirm=True,
    )
    _, vars_put = fc.calls[1]
    assert vars_put["input"]["seo"] == {"description": "Only desc"}, vars_put


def test_seo_user_errors_surfaced():
    tools, fc = _build([_seo_read(), _update_err("seo.title", "must be a string")])
    out = tools["update_product_seo"](
        product_id="123", new_seo_title="x", confirm=True,
    )
    assert out.startswith("Error:") and "must be a string" in out, out


def _between(text, label, next_label=None):
    """Return the content between `label` and the next label (or end)."""
    start = text.index(label) + len(label)
    end = text.index(next_label, start) if next_label else len(text)
    return text[start:end]


def test_seo_preview_shows_old_empty_and_unchanged_field():
    tools, fc = _build([_seo_read()])
    out = tools["update_product_seo"](
        product_id="123", new_seo_title="Fresh title", confirm=False,
    )
    # Behavioral check — not coupled to label column width.
    old_title_val = _between(out, "Old SEO title", "New SEO title")
    old_desc_val = _between(out, "Old SEO description", "New SEO description")
    new_desc_val = _between(out, "New SEO description", "\n\n")
    assert "(empty)" in old_title_val, out
    assert "(empty)" in old_desc_val, out
    assert "(unchanged)" in new_desc_val, out


# ---------- update_product_title handle logic ----------

PROD_ID = "7330113421465"
CUR_TITLE = "Iconic V Logo Pull Over Hoodie"
CUR_HANDLE = "iconic-v-logo-pull-over-hoodie"


def test_title_change_handle_false_preserves_handle_explicitly():
    tools, fc = _build([
        _product_read(PROD_ID, CUR_TITLE, CUR_HANDLE),
        _update_ok(pid=PROD_ID),
    ])
    out = tools["update_product_title"](
        product_id=PROD_ID,
        new_title="Totally Different Title",
        change_handle=False,
        confirm=True,
    )
    assert "UNCHANGED (preserved; change_handle=False)" in out, out
    assert fc.calls[1][1]["input"]["handle"] == CUR_HANDLE
    assert fc.calls[0][0] == GET_PRODUCT_BY_ID
    assert fc.calls[1][0] == UPDATE_PRODUCT


def test_title_change_handle_true_slug_matches_shows_unchanged():
    tools, fc = _build([
        _product_read(PROD_ID, CUR_TITLE, CUR_HANDLE),
        _update_ok(pid=PROD_ID),
    ])
    out = tools["update_product_title"](
        product_id=PROD_ID,
        new_title='Iconic "V" Logo Pull Over Hoodie',  # quotes stripped -> same slug
        change_handle=True,
        confirm=True,
    )
    assert f"UNCHANGED (new slug matches existing: {CUR_HANDLE})" in out, out
    assert fc.calls[1][1]["input"]["handle"] == CUR_HANDLE


def test_title_change_handle_true_slug_differs_shows_old_new_pair():
    tools, fc = _build([
        _product_read(PROD_ID, CUR_TITLE, CUR_HANDLE),
        _update_ok(pid=PROD_ID),
    ])
    out = tools["update_product_title"](
        product_id=PROD_ID,
        new_title="Iconic V Crewneck",
        change_handle=True,
        confirm=False,
    )
    assert f"Old handle : {CUR_HANDLE}" in out, out
    assert "New handle : iconic-v-crewneck" in out, out


def test_title_user_errors_surfaced():
    tools, fc = _build([
        _product_read(PROD_ID, CUR_TITLE, CUR_HANDLE),
        _update_err("handle", "has already been taken"),
    ])
    out = tools["update_product_title"](
        product_id=PROD_ID,
        new_title="Iconic V Crewneck",
        change_handle=True,
        confirm=True,
    )
    assert out.startswith("Error:") and "has already been taken" in out, out


# ---------- List / collection response-unwrap regressions ----------

def _product_summary(pid, title, handle, status="ACTIVE", variants=None):
    return {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "handle": handle,
        "status": status,
        "variants": {"nodes": variants or []},
    }


def _variant(vid, title):
    return {"id": f"gid://shopify/ProductVariant/{vid}", "title": title}


def _product_with_body(pid, title, handle, body, status="ACTIVE"):
    return {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "handle": handle,
        "status": status,
        "bodyHtml": body,
    }


def test_get_products_unwraps_nodes_list():
    response = {"products": {"nodes": [
        _product_summary("111", "Tee One", "tee-one", variants=[_variant("11", "Black")]),
        _product_summary("222", "Tee Two", "tee-two", variants=[_variant("22", "White")]),
    ]}}
    tools, fc = _build([response])
    out = tools["get_products"]()
    assert "[111] Tee One" in out and "handle: tee-one" in out and "status: ACTIVE" in out
    assert "[222] Tee Two" in out
    assert "Black (id:11)" in out and "White (id:22)" in out
    assert fc.calls[0][0] == GET_PRODUCTS
    assert fc.calls[0][1] == {"first": 250}


def test_get_products_empty_returns_message():
    tools, fc = _build([{"products": {"nodes": []}}])
    out = tools["get_products"]()
    assert out == "No products found."


def test_get_products_by_collection_unwraps_nested_nodes():
    response = {"collectionByHandle": {
        "id": "gid://shopify/Collection/999",
        "title": "Vanish",
        "handle": "vanish",
        "products": {"nodes": [
            _product_summary("111", "Tee One", "tee-one"),
            _product_summary("222", "Tee Two", "tee-two"),
        ]},
    }}
    tools, fc = _build([response])
    out = tools["get_products_by_collection"](collection_handle="vanish")
    assert "Products in 'vanish' (2 total)" in out
    assert "[111] Tee One" in out and "[222] Tee Two" in out
    assert fc.calls[0][0] == GET_PRODUCTS_BY_COLLECTION
    assert fc.calls[0][1] == {"handle": "vanish", "first": 250}


def test_get_products_by_collection_handle_not_found():
    tools, fc = _build([{"collectionByHandle": None}])
    out = tools["get_products_by_collection"](collection_handle="nope")
    assert out == "No collection found with handle 'nope'."


def test_get_products_by_collection_empty_collection():
    response = {"collectionByHandle": {
        "id": "gid://shopify/Collection/999",
        "title": "Empty",
        "handle": "empty",
        "products": {"nodes": []},
    }}
    tools, fc = _build([response])
    out = tools["get_products_by_collection"](collection_handle="empty")
    assert out == "No products in collection 'empty'."


def test_get_products_with_descriptions_scoped_to_collection():
    response = {"collectionByHandle": {
        "id": "gid://shopify/Collection/999",
        "title": "Vanish",
        "handle": "vanish",
        "products": {"nodes": [
            _product_with_body("111", "Tee One", "tee-one", "<p>body one</p>"),
        ]},
    }}
    tools, fc = _build([response])
    out = tools["get_products_with_descriptions"](collection_handle="vanish", limit=25)
    assert "Products in 'vanish' (1 total)" in out
    assert "ID: 111" in out and "<p>body one</p>" in out
    assert fc.calls[0][0] == GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS
    assert fc.calls[0][1] == {"handle": "vanish", "first": 25}


def test_get_products_with_descriptions_unscoped_bulk():
    response = {"products": {"nodes": [
        _product_with_body("111", "Tee One", "tee-one", "<p>one</p>"),
        _product_with_body("222", "Tee Two", "tee-two", "<p>two</p>"),
    ]}}
    tools, fc = _build([response])
    out = tools["get_products_with_descriptions"](limit=10)
    assert "Products (2 total)" in out
    assert "<p>one</p>" in out and "<p>two</p>" in out
    assert fc.calls[0][0] == GET_PRODUCTS_WITH_DESCRIPTIONS
    assert fc.calls[0][1] == {"first": 10}


def test_get_products_with_descriptions_collection_not_found():
    tools, fc = _build([{"collectionByHandle": None}])
    out = tools["get_products_with_descriptions"](collection_handle="missing", limit=10)
    assert out == "No collection found with handle 'missing'."


def test_get_products_with_descriptions_limit_clamped_high():
    response = {"products": {"nodes": []}}
    tools, fc = _build([response])
    tools["get_products_with_descriptions"](limit=9999)
    assert fc.calls[0][1] == {"first": 250}


def test_get_products_with_descriptions_limit_clamped_low():
    response = {"products": {"nodes": []}}
    tools, fc = _build([response])
    tools["get_products_with_descriptions"](limit=0)
    assert fc.calls[0][1] == {"first": 1}


# ---------- update_product_tags ----------

def _full_read_with_tags(tags, pid="123"):
    return {"product": {
        "id": f"gid://shopify/Product/{pid}",
        "title": "T",
        "handle": "t",
        "status": "ACTIVE",
        "bodyHtml": "",
        "tags": list(tags),
        "productType": "",
        "vendor": "",
        "seo": {"title": "", "description": ""},
        "variants": {"nodes": []},
    }}


def _tags_update_ok(pid="123", tags=None):
    return {"productUpdate": {
        "product": {"id": f"gid://shopify/Product/{pid}", "tags": tags or []},
        "userErrors": [],
    }}


def _tags_update_err(field, message):
    return {"productUpdate": {"product": None, "userErrors": [{"field": field, "message": message}]}}


def test_tags_replace_skips_pre_read_and_writes_verbatim():
    tools, fc = _build([_tags_update_ok(tags=["vaulted"])])
    out = tools["update_product_tags"](
        product_id="123", new_tags=["vaulted"], mode="replace", confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    # replace mode: NO pre-read, only the UPDATE call.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == UPDATE_PRODUCT_TAGS
    assert fc.calls[0][1]["input"] == {
        "id": "gid://shopify/Product/123",
        "tags": ["vaulted"],
    }


def test_tags_append_dedupes_and_preserves_order():
    tools, fc = _build([
        _full_read_with_tags(["vanish-clothing", "april-drop"]),
        _tags_update_ok(),
    ])
    tools["update_product_tags"](
        product_id="123",
        new_tags=["april-drop", "vaulted"],
        mode="append",
        confirm=True,
    )
    assert fc.calls[1][1]["input"]["tags"] == [
        "vanish-clothing", "april-drop", "vaulted",
    ]


def test_tags_remove_strips_only_named():
    tools, fc = _build([
        _full_read_with_tags(["vanish-clothing", "vaulted", "april-drop"]),
        _tags_update_ok(),
    ])
    tools["update_product_tags"](
        product_id="123",
        new_tags=["vaulted"],
        mode="remove",
        confirm=True,
    )
    assert fc.calls[1][1]["input"]["tags"] == ["vanish-clothing", "april-drop"]


def test_tags_preview_does_not_call_mutation():
    tools, fc = _build([_full_read_with_tags(["vanish-clothing"])])
    out = tools["update_product_tags"](
        product_id="123",
        new_tags=["vaulted"],
        mode="append",
        confirm=False,
    )
    assert out.startswith("PREVIEW —"), out
    assert "confirm=True" in out
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_FULL_BY_ID


def test_tags_empty_new_tags_rejected_no_shopify_call():
    tools, fc = _build([])
    out = tools["update_product_tags"](
        product_id="123", new_tags=[], mode="replace", confirm=True,
    )
    assert out.startswith("Error:"), out
    assert fc.calls == []


def test_tags_invalid_mode_rejected_no_shopify_call():
    tools, fc = _build([])
    out = tools["update_product_tags"](
        product_id="123", new_tags=["x"], mode="replace_all", confirm=True,
    )
    assert out.startswith("Error:"), out
    assert fc.calls == []


def test_tags_user_errors_surfaced():
    tools, fc = _build([_tags_update_err("tags", "invalid tag")])
    out = tools["update_product_tags"](
        product_id="123", new_tags=["x"], mode="replace", confirm=True,
    )
    assert out.startswith("Error:") and "invalid tag" in out, out


def test_tags_append_preview_shows_added_and_not_removed():
    tools, fc = _build([_full_read_with_tags(["vanish-clothing", "old-tag"])])
    out = tools["update_product_tags"](
        product_id="123",
        new_tags=["vaulted"],
        mode="append",
        confirm=False,
    )
    assert "Added" in out and "vaulted" in out
    # append never removes existing tags.
    assert "Removed     : (none)" in out or "Removed    : (none)" in out


# ---------- update_product_status ----------

def _status_update_ok(pid="123", status="ACTIVE"):
    return {"productUpdate": {
        "product": {"id": f"gid://shopify/Product/{pid}", "status": status},
        "userErrors": [],
    }}


def test_status_valid_transition_mutation_shape():
    tools, fc = _build([
        _product_read("123", "T", "t"),
        _status_update_ok(status="ARCHIVED"),
    ])
    out = tools["update_product_status"](
        product_id="123", new_status="ARCHIVED", confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert fc.calls[0][0] == GET_PRODUCT_BY_ID
    assert fc.calls[1][0] == UPDATE_PRODUCT_STATUS
    assert fc.calls[1][1]["input"] == {
        "id": "gid://shopify/Product/123",
        "status": "ARCHIVED",
    }


def test_status_invalid_value_rejected_no_shopify_call():
    tools, fc = _build([])
    out = tools["update_product_status"](
        product_id="123", new_status="paused", confirm=True,
    )
    assert out.startswith("Error:"), out
    assert fc.calls == []


def test_status_preview_does_not_call_mutation():
    tools, fc = _build([_product_read("123", "T", "t")])
    out = tools["update_product_status"](
        product_id="123", new_status="DRAFT", confirm=False,
    )
    assert out.startswith("PREVIEW —")
    assert "confirm=True" in out
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_BY_ID


def test_status_no_op_note_shown_when_target_matches_current():
    tools, fc = _build([_product_read("123", "T", "t")])  # read returns status=ACTIVE
    out = tools["update_product_status"](
        product_id="123", new_status="ACTIVE", confirm=False,
    )
    assert "no-op" in out, out


def test_status_user_errors_surfaced():
    tools, fc = _build([
        _product_read("123", "T", "t"),
        _tags_update_err("status", "something broke"),
    ])
    out = tools["update_product_status"](
        product_id="123", new_status="ARCHIVED", confirm=True,
    )
    assert out.startswith("Error:") and "something broke" in out, out


# ---------- update_variant_inventory_policy ----------

def _variants_policy_read(variants, pid="123", title="T"):
    return {"product": {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "variants": {"nodes": variants},
    }}


def _variant_policy(vid, title, policy):
    return {
        "id": f"gid://shopify/ProductVariant/{vid}",
        "title": title,
        "inventoryPolicy": policy,
    }


def _bulk_policy_ok(updated_variants):
    return {"productVariantsBulkUpdate": {
        "product": {"id": "gid://shopify/Product/123"},
        "productVariants": updated_variants,
        "userErrors": [],
    }}


def _bulk_policy_err(field, message):
    return {"productVariantsBulkUpdate": {
        "product": None,
        "productVariants": [],
        "userErrors": [{"field": field, "message": message}],
    }}


def test_policy_all_variants_default_applies_to_every_variant():
    variants = [
        _variant_policy("10", "S", "CONTINUE"),
        _variant_policy("11", "M", "CONTINUE"),
        _variant_policy("12", "L", "CONTINUE"),
    ]
    updated = [
        {"id": v["id"], "inventoryPolicy": "DENY"} for v in variants
    ]
    tools, fc = _build([
        _variants_policy_read(variants),
        _bulk_policy_ok(updated),
    ])
    out = tools["update_variant_inventory_policy"](
        product_id="123", new_policy="DENY", confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert fc.calls[0][0] == GET_PRODUCT_VARIANTS_POLICY
    assert fc.calls[1][0] == UPDATE_PRODUCT_VARIANTS_POLICY
    vars_put = fc.calls[1][1]
    assert vars_put["productId"] == "gid://shopify/Product/123"
    assert vars_put["variants"] == [
        {"id": "gid://shopify/ProductVariant/10", "inventoryPolicy": "DENY"},
        {"id": "gid://shopify/ProductVariant/11", "inventoryPolicy": "DENY"},
        {"id": "gid://shopify/ProductVariant/12", "inventoryPolicy": "DENY"},
    ]


def test_policy_explicit_variant_ids_filter_matches_only_listed():
    variants = [
        _variant_policy("10", "S", "CONTINUE"),
        _variant_policy("11", "M", "CONTINUE"),
        _variant_policy("12", "L", "CONTINUE"),
    ]
    tools, fc = _build([
        _variants_policy_read(variants),
        _bulk_policy_ok([{"id": "gid://shopify/ProductVariant/11",
                          "inventoryPolicy": "DENY"}]),
    ])
    tools["update_variant_inventory_policy"](
        product_id="123",
        new_policy="DENY",
        variant_ids=["11"],
        confirm=True,
    )
    vars_put = fc.calls[1][1]
    assert vars_put["variants"] == [
        {"id": "gid://shopify/ProductVariant/11", "inventoryPolicy": "DENY"},
    ]


def test_policy_unknown_variant_id_reported_and_not_sent():
    variants = [_variant_policy("10", "S", "CONTINUE")]
    tools, fc = _build([_variants_policy_read(variants)])
    out = tools["update_variant_inventory_policy"](
        product_id="123",
        new_policy="DENY",
        variant_ids=["9999"],
        confirm=False,
    )
    assert "Unresolved" in out and "9999" in out
    # Only the read should have happened in preview mode.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_VARIANTS_POLICY


def test_policy_unknown_variant_id_on_confirm_skips_mutation_when_no_targets():
    variants = [_variant_policy("10", "S", "CONTINUE")]
    tools, fc = _build([_variants_policy_read(variants)])
    out = tools["update_variant_inventory_policy"](
        product_id="123",
        new_policy="DENY",
        variant_ids=["9999"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert "no-op" in out
    assert "9999" in out
    # Only the read happened — no bulk update issued when nothing resolves.
    assert len(fc.calls) == 1


def test_policy_invalid_enum_rejected_no_shopify_call():
    tools, fc = _build([])
    out = tools["update_variant_inventory_policy"](
        product_id="123", new_policy="STOP", confirm=True,
    )
    assert out.startswith("Error:"), out
    assert fc.calls == []


def test_policy_preview_does_not_call_mutation():
    variants = [_variant_policy("10", "S", "CONTINUE")]
    tools, fc = _build([_variants_policy_read(variants)])
    out = tools["update_variant_inventory_policy"](
        product_id="123", new_policy="DENY", confirm=False,
    )
    assert out.startswith("PREVIEW —")
    assert "confirm=True" in out
    assert len(fc.calls) == 1


def test_policy_user_errors_surfaced():
    variants = [_variant_policy("10", "S", "CONTINUE")]
    tools, fc = _build([
        _variants_policy_read(variants),
        _bulk_policy_err(["input", "0", "inventoryPolicy"], "invalid value"),
    ])
    out = tools["update_variant_inventory_policy"](
        product_id="123", new_policy="DENY", confirm=True,
    )
    assert out.startswith("Error:") and "invalid value" in out, out


# ---------- No-op passthrough coverage ----------

def test_tags_append_all_existing_still_writes_unchanged_list():
    """append when every new tag already exists (case-insensitive) still
    issues the write — mirrors the existing no-op discipline of sibling
    update_product_* tools that always write on confirm=True."""
    tools, fc = _build([
        _full_read_with_tags(["vanish-clothing", "vaulted"]),
        _tags_update_ok(),
    ])
    tools["update_product_tags"](
        product_id="123",
        new_tags=["vaulted"],
        mode="append",
        confirm=True,
    )
    assert fc.calls[1][1]["input"]["tags"] == ["vanish-clothing", "vaulted"]


def test_tags_remove_no_match_still_writes_unchanged_list():
    tools, fc = _build([
        _full_read_with_tags(["vanish-clothing", "april-drop"]),
        _tags_update_ok(),
    ])
    tools["update_product_tags"](
        product_id="123",
        new_tags=["does-not-exist"],
        mode="remove",
        confirm=True,
    )
    assert fc.calls[1][1]["input"]["tags"] == ["vanish-clothing", "april-drop"]


def test_tags_replace_identical_list_still_writes():
    """replace skips the pre-read, so a no-op replace still issues one write
    with the same list (acceptable — single round-trip, preview-unless-confirm)."""
    tools, fc = _build([_tags_update_ok()])
    tools["update_product_tags"](
        product_id="123",
        new_tags=["vanish-clothing", "vaulted"],
        mode="replace",
        confirm=True,
    )
    assert len(fc.calls) == 1
    assert fc.calls[0][1]["input"]["tags"] == ["vanish-clothing", "vaulted"]


def test_policy_product_with_no_variants_skips_mutation():
    """All-variants mode on a product with zero variants hits the no-op branch
    — mutation is not issued, and the CONFIRMED response notes the no-op."""
    tools, fc = _build([_variants_policy_read([])])
    out = tools["update_variant_inventory_policy"](
        product_id="123", new_policy="DENY", confirm=True,
    )
    assert out.startswith("CONFIRMED —") and "no-op" in out
    assert len(fc.calls) == 1  # only the read


# ---------- Coverage for the code-review polish pass ----------

def test_tags_append_is_case_insensitive_existing_casing_wins():
    """#2: appending 'Vaulted' to existing ['vaulted'] must not duplicate —
    existing lowercase is preserved."""
    tools, fc = _build([
        _full_read_with_tags(["vaulted", "vanish-clothing"]),
        _tags_update_ok(),
    ])
    tools["update_product_tags"](
        product_id="123",
        new_tags=["Vaulted", "April-Drop"],
        mode="append",
        confirm=True,
    )
    # 'Vaulted' collides with 'vaulted' (existing wins); 'April-Drop' is new.
    assert fc.calls[1][1]["input"]["tags"] == [
        "vaulted", "vanish-clothing", "April-Drop",
    ]


def test_tags_remove_is_case_insensitive():
    """#2: removing 'VAULTED' strips 'vaulted' regardless of casing."""
    tools, fc = _build([
        _full_read_with_tags(["vaulted", "vanish-clothing"]),
        _tags_update_ok(),
    ])
    tools["update_product_tags"](
        product_id="123",
        new_tags=["VAULTED"],
        mode="remove",
        confirm=True,
    )
    assert fc.calls[1][1]["input"]["tags"] == ["vanish-clothing"]


def test_policy_unresolved_variant_ids_are_deduped():
    """#3: duplicate variant_ids in the input don't produce duplicate entries
    in the unresolved list."""
    variants = [_variant_policy("10", "S", "CONTINUE")]
    tools, fc = _build([_variants_policy_read(variants)])
    out = tools["update_variant_inventory_policy"](
        product_id="123",
        new_policy="DENY",
        variant_ids=["9999", "9999"],
        confirm=False,
    )
    # 9999 should appear exactly once in the unresolved block.
    assert out.count("9999") == 1, out


def test_policy_user_errors_surfaced_with_null_field():
    """#4: userError with field=None must not crash and must format cleanly."""
    variants = [_variant_policy("10", "S", "CONTINUE")]
    tools, fc = _build([
        _variants_policy_read(variants),
        _bulk_policy_err(None, "something went wrong"),
    ])
    out = tools["update_variant_inventory_policy"](
        product_id="123", new_policy="DENY", confirm=True,
    )
    assert out.startswith("Error:"), out
    assert "something went wrong" in out
    assert "(no field)" in out


def test_policy_at_cap_warning_surfaces_when_reading_250_variants():
    """#1: when the variants read returns exactly 250 nodes (Shopify page cap),
    the response must include the at-cap warning so operators see a product
    that has exceeded Shopify's single-request ceiling."""
    variants = [_variant_policy(str(1000 + i), f"v{i}", "CONTINUE")
                for i in range(250)]
    tools, fc = _build([_variants_policy_read(variants)])
    out = tools["update_variant_inventory_policy"](
        product_id="123", new_policy="DENY", confirm=False,
    )
    assert "WARNING" in out and "250-variant" in out, out


# ---------- get_product_collections ----------

def _collection_node(cid, handle, title, smart=False):
    return {
        "id": f"gid://shopify/Collection/{cid}",
        "handle": handle,
        "title": title,
        "ruleSet": {"appliedDisjunctively": False} if smart else None,
    }


def _product_collections_read(pid, title, nodes, has_next=False):
    return {"product": {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "collections": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": has_next},
        },
    }}


def test_get_product_collections_product_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["get_product_collections"](product_id="999")
    assert out == "No product found with id 999."
    assert fc.calls[0][0] == GET_PRODUCT_COLLECTIONS


def test_get_product_collections_empty_list():
    tools, fc = _build([_product_collections_read("123", "Hoodie", [])])
    out = tools["get_product_collections"](product_id="123")
    assert "Product: Hoodie (id: 123)" in out
    assert "Collections (0 total): (none)" in out


def test_get_product_collections_manual_only():
    nodes = [
        _collection_node("10", "fall-2025-merch", "Fall 2025"),
        _collection_node("11", "vanish", "Vanish Clothing"),
    ]
    tools, fc = _build([_product_collections_read("123", "Hoodie", nodes)])
    out = tools["get_product_collections"](product_id="123")
    assert "Collections (2 total):" in out
    assert "Fall 2025 (handle: fall-2025-merch, id: 10, type: manual)" in out
    assert "Vanish Clothing (handle: vanish, id: 11, type: manual)" in out
    assert "type: smart" not in out


def test_get_product_collections_mixed_manual_and_smart():
    """A smart collection has a ruleSet; a manual one does not. The type label
    must differ between them."""
    nodes = [
        _collection_node("10", "vanish", "Vanish Clothing", smart=False),
        _collection_node("20", "best-sellers", "Best Sellers", smart=True),
    ]
    tools, fc = _build([_product_collections_read("123", "Hoodie", nodes)])
    out = tools["get_product_collections"](product_id="123")
    assert "Collections (2 total):" in out
    assert "Vanish Clothing (handle: vanish, id: 10, type: manual)" in out
    assert "Best Sellers (handle: best-sellers, id: 20, type: smart)" in out


def test_get_product_collections_uses_product_gid():
    tools, fc = _build([_product_collections_read("8559386689689", "X", [])])
    tools["get_product_collections"](product_id="8559386689689")
    _, vars_ = fc.calls[0]
    assert vars_ == {"id": "gid://shopify/Product/8559386689689"}


def test_get_product_collections_warns_when_more_pages_available():
    """hasNextPage=True → surface an at-cap warning so operators know the list
    is truncated. The whole point of this tool is completeness on the vault
    path; a silent truncation re-introduces the bug it was built to fix."""
    nodes = [_collection_node(str(i), f"c-{i}", f"Col {i}") for i in range(3)]
    tools, fc = _build([_product_collections_read("123", "X", nodes, has_next=True)])
    out = tools["get_product_collections"](product_id="123")
    assert "WARNING" in out
    assert "250" in out


def test_get_product_collections_no_warning_when_all_on_one_page():
    nodes = [_collection_node("10", "c-10", "Col 10")]
    tools, fc = _build([_product_collections_read("123", "X", nodes, has_next=False)])
    out = tools["get_product_collections"](product_id="123")
    assert "WARNING" not in out


# ---------- get_product ----------

def _full_product(pid, title, handle, status="ACTIVE", body="<p>b</p>",
                  variants=None, tags=None, seo=None,
                  product_type=None, vendor=None):
    return {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "handle": handle,
        "status": status,
        "bodyHtml": body,
        "variants": {"nodes": variants or []},
        "tags": tags or [],
        "seo": seo or {},
        "productType": product_type,
        "vendor": vendor,
    }


def test_get_product_by_id_renders_variants():
    tools, fc = _build([{"product": _full_product(
        "123", "Tee", "tee",
        variants=[{"id": "gid://shopify/ProductVariant/1", "title": "S", "sku": "T-S"}],
    )}])
    out = tools["get_product"](product_id="123")
    assert "ID: 123" in out and "Title: Tee" in out and "Handle: tee" in out
    assert "SKU: T-S" in out
    assert fc.calls[0][0] == GET_PRODUCT_BY_ID


def test_get_product_by_handle_uses_handle_query():
    tools, fc = _build([{"productByHandle": _full_product("9", "X", "x")}])
    out = tools["get_product"](handle="x")
    assert "Title: X" in out
    assert fc.calls[0][0] == GET_PRODUCT_BY_HANDLE
    assert fc.calls[0][1] == {"handle": "x"}


def test_get_product_requires_id_or_handle():
    tools, fc = _build([])
    out = tools["get_product"]()
    assert out == "Provide either product_id or handle."
    assert fc.calls == []


def test_get_product_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["get_product"](product_id="nope")
    assert out == "No product found."


def test_get_product_variant_sku_falls_back_to_na():
    tools, fc = _build([{"product": _full_product(
        "123", "Tee", "tee",
        variants=[{"id": "gid://shopify/ProductVariant/1", "title": "S"}],
    )}])
    out = tools["get_product"](product_id="123")
    assert "SKU: N/A" in out


# ---------- get_product_description ----------

def test_get_product_description_by_id():
    tools, fc = _build([{"product": _full_product("7", "Tee", "tee", body="<p>hi</p>")}])
    out = tools["get_product_description"](product_id="7")
    assert "body_html:\n<p>hi</p>" in out
    assert fc.calls[0][0] == GET_PRODUCT_BY_ID


def test_get_product_description_by_handle():
    tools, fc = _build([{"productByHandle": _full_product("7", "Tee", "tee", body="<p>h</p>")}])
    out = tools["get_product_description"](handle="tee")
    assert "Handle: tee" in out
    assert fc.calls[0][0] == GET_PRODUCT_BY_HANDLE


def test_get_product_description_requires_id_or_handle():
    tools, fc = _build([])
    out = tools["get_product_description"]()
    assert out == "Provide either product_id or handle."
    assert fc.calls == []


def test_get_product_description_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["get_product_description"](product_id="nope")
    assert out == "No product found."


def test_get_product_description_empty_body_renders_empty_string():
    """bodyHtml=None should render as empty — not as the literal 'None'."""
    tools, fc = _build([{"product": _full_product("7", "Tee", "tee", body=None)}])
    out = tools["get_product_description"](product_id="7")
    assert out.endswith("body_html:\n")


# ---------- get_product_full ----------

def test_get_product_full_by_id_renders_all_fields():
    tools, fc = _build([{"product": _full_product(
        "123", "Hoodie", "hoodie", status="DRAFT",
        variants=[{"id": "gid://shopify/ProductVariant/1", "title": "S", "sku": "H-S"}],
        tags=["new", "sale"],
        seo={"title": "SEO T", "description": "SEO D"},
        product_type="Apparel",
        vendor="AON",
    )}])
    out = tools["get_product_full"](product_id="123")
    assert "Title: Hoodie" in out
    assert "Status: DRAFT" in out
    assert "Product type: Apparel" in out
    assert "Vendor: AON" in out
    assert "Tags: new, sale" in out
    assert "SEO title: SEO T" in out
    assert "SEO description: SEO D" in out
    assert "S — SKU: H-S" in out
    assert fc.calls[0][0] == GET_PRODUCT_FULL_BY_ID


def test_get_product_full_by_handle_uses_handle_query():
    tools, fc = _build([{"productByHandle": _full_product("9", "X", "x")}])
    out = tools["get_product_full"](handle="x")
    assert "Title: X" in out
    assert fc.calls[0][0] == GET_PRODUCT_FULL_BY_HANDLE


def test_get_product_full_requires_id_or_handle():
    tools, fc = _build([])
    out = tools["get_product_full"]()
    assert out == "Provide either product_id or handle."


def test_get_product_full_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["get_product_full"](product_id="nope")
    assert out == "No product found."


def test_get_product_full_empty_optionals_render_placeholders():
    tools, fc = _build([{"product": _full_product("7", "T", "t")}])
    out = tools["get_product_full"](product_id="7")
    assert "Product type: (none)" in out
    assert "Vendor: (none)" in out
    assert "Tags: (none)" in out
    assert "SEO title: (none)" in out
    assert "SEO description: (none)" in out


# ---------- update_product_description ----------

def test_update_description_preview_shows_old_and_new_excerpts_no_mutation():
    tools, fc = _build([{"product": {"bodyHtml": "<p>old body</p>"}}])
    out = tools["update_product_description"](
        product_id="7", new_description="<p>new body</p>",
    )
    assert "PREVIEW" in out
    assert "old body" in out and "new body" in out
    assert "confirm=True" in out
    # Only the pre-read; no mutation
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_BY_ID


def test_update_description_truncates_long_excerpts_with_ellipsis():
    long_old = "x" * 200
    long_new = "y" * 200
    tools, fc = _build([{"product": {"bodyHtml": long_old}}])
    out = tools["update_product_description"](
        product_id="7", new_description=long_new,
    )
    # Excerpt capped at 120 chars + ellipsis
    assert "x" * 120 + "..." in out
    assert "y" * 120 + "..." in out


def test_update_description_confirm_sends_update_mutation():
    tools, fc = _build([
        {"product": {"bodyHtml": "<p>old</p>"}},
        _update_ok(pid="7"),
    ])
    out = tools["update_product_description"](
        product_id="7", new_description="<p>new</p>", confirm=True,
    )
    assert out.startswith("Done.")
    query, vars_ = fc.calls[1]
    assert query == UPDATE_PRODUCT
    assert vars_["input"] == {
        "id": "gid://shopify/Product/7",
        "descriptionHtml": "<p>new</p>",
    }


def test_update_description_user_errors_surfaced():
    tools, fc = _build([
        {"product": {"bodyHtml": ""}},
        _update_err("descriptionHtml", "invalid html"),
    ])
    out = tools["update_product_description"](
        product_id="7", new_description="<script>", confirm=True,
    )
    assert out.startswith("Error:")
    assert "descriptionHtml: invalid html" in out


# ---------- not-found branches for update_product_seo / tags(append) / status / policy ----------

def test_update_product_seo_not_found_reports_clean_error():
    tools, fc = _build([{"product": None}])
    out = tools["update_product_seo"](
        product_id="nope", new_seo_title="T", confirm=True,
    )
    assert out == "No product found with id nope."


def test_update_product_tags_append_not_found_reports_clean_error():
    """append/remove modes pre-read to compute the diff. If the read returns
    no product, surface a clean error — don't attempt a mutation against a
    non-existent id."""
    tools, fc = _build([{"product": None}])
    out = tools["update_product_tags"](
        product_id="nope", mode="append", new_tags=["x"], confirm=True,
    )
    assert out == "No product found with id nope."
    # Only the pre-read ran.
    assert len(fc.calls) == 1


def test_update_product_tags_remove_not_found_reports_clean_error():
    tools, fc = _build([{"product": None}])
    out = tools["update_product_tags"](
        product_id="nope", mode="remove", new_tags=["x"], confirm=True,
    )
    assert out == "No product found with id nope."


def test_update_product_status_not_found_reports_clean_error():
    tools, fc = _build([{"product": None}])
    out = tools["update_product_status"](
        product_id="nope", new_status="ACTIVE", confirm=True,
    )
    assert out == "No product found with id nope."


def test_update_variant_inventory_policy_not_found_reports_clean_error():
    tools, fc = _build([{"product": None}])
    out = tools["update_variant_inventory_policy"](
        product_id="nope", new_policy="DENY", confirm=True,
    )
    assert out == "No product found with id nope."
