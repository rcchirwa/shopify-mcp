"""
Offline unit tests for tools/products.py write paths.

Uses a fake client to exercise preview/mutation-shape branches without
hitting Shopify. Covers update_product_seo and the handle logic in
update_product_title.

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
    GET_PRODUCT_SEO_BY_ID,
    UPDATE_PRODUCT,
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
