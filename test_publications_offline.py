"""
Offline unit tests for tools/publications.py.

Uses a scripted FakeClient to exercise name resolution, preview/confirm,
idempotency, and the declarative set_product_publications diff.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_publications_offline.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from tools import publications
from tools.publications import (
    LIST_PUBLICATIONS,
    GET_PRODUCT_PUBLICATIONS_BY_ID,
    GET_PRODUCT_PUBLICATIONS_BY_HANDLE,
    PUBLISHABLE_PUBLISH,
    PUBLISHABLE_UNPUBLISH,
)


class CapturingServer:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
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
    publications.register(srv, fc)
    return srv.tools, fc


# ---- Fixture channels ----
ONLINE = {"id": "gid://shopify/Publication/1", "name": "Online Store", "supportsFuturePublishing": True}
POS = {"id": "gid://shopify/Publication/2", "name": "Point of Sale", "supportsFuturePublishing": False}
SHOP = {"id": "gid://shopify/Publication/3", "name": "Shop", "supportsFuturePublishing": True}
GOOGLE = {"id": "gid://shopify/Publication/4", "name": "Google & YouTube", "supportsFuturePublishing": True}

ALL_CHANNELS = [ONLINE, POS, SHOP, GOOGLE]


def _channels_response(nodes=None):
    return {"publications": {"nodes": nodes if nodes is not None else ALL_CHANNELS}}


def _product_pubs(pid="123", title="Tee", handle="tee", published_ids=None, not_published_ids=None):
    published_ids = published_ids or []
    not_published_ids = not_published_ids or []
    nodes = []
    for i in published_ids:
        nodes.append({
            "publication": {"id": f"gid://shopify/Publication/{i}", "name": _name_for(i)},
            "publishDate": "2026-04-20T10:00:00Z",
            "isPublished": True,
        })
    for i in not_published_ids:
        nodes.append({
            "publication": {"id": f"gid://shopify/Publication/{i}", "name": _name_for(i)},
            "publishDate": None,
            "isPublished": False,
        })
    return {"product": {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "handle": handle,
        "resourcePublications": {"nodes": nodes},
    }}


def _name_for(i):
    for c in ALL_CHANNELS:
        if c["id"].endswith(f"/{i}"):
            return c["name"]
    return "?"


def _publish_ok():
    return {"publishablePublish": {
        "publishable": {"id": "gid://shopify/Product/123", "title": "Tee"},
        "userErrors": [],
    }}


def _publish_err(field, message):
    return {"publishablePublish": {"publishable": None, "userErrors": [{"field": field, "message": message}]}}


def _unpublish_ok():
    return {"publishableUnpublish": {
        "publishable": {"id": "gid://shopify/Product/123", "title": "Tee"},
        "userErrors": [],
    }}


# ---------- list_sales_channels ----------

def test_list_sales_channels_returns_all():
    tools, fc = _build([_channels_response()])
    out = tools["list_sales_channels"]()
    assert "4 total" in out
    assert "Online Store" in out and "Point of Sale" in out and "Shop" in out
    assert "supports_future_publishing: yes" in out
    assert "supports_future_publishing: no" in out  # POS
    assert fc.calls[0][0] == LIST_PUBLICATIONS


def test_list_sales_channels_empty():
    tools, fc = _build([_channels_response([])])
    out = tools["list_sales_channels"]()
    assert "No sales channels" in out


def test_list_sales_channels_scope_error_hint():
    class Exploding:
        def execute(self, q, v=None):
            raise RuntimeError("Shopify GraphQL error: Access denied for publications")
    srv = CapturingServer()
    publications.register(srv, Exploding())
    out = srv.tools["list_sales_channels"]()
    assert "Access denied" in out
    assert "read_publications" in out


# ---------- get_product_publications ----------

def test_get_product_publications_split_published_vs_not():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1, 3], not_published_ids=[2, 4]),
    ])
    out = tools["get_product_publications"](product_id="123")
    assert "Published to (2)" in out
    assert "Not published to (2)" in out
    # Order doesn't matter, so just check names appear under right sections.
    pub_section = out[out.index("Published to"):out.index("Not published to")]
    not_section = out[out.index("Not published to"):]
    assert "Online Store" in pub_section and "Shop" in pub_section
    assert "Point of Sale" in not_section and "Google & YouTube" in not_section


def test_get_product_publications_derives_not_published_from_channels_list():
    """If Shopify's resourcePublications only lists published, we still fill in the rest."""
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[]),
    ])
    out = tools["get_product_publications"](product_id="123")
    # not_published = all channels - published = {2, 3, 4}
    not_section = out[out.index("Not published to"):]
    assert "Point of Sale" in not_section
    assert "Shop" in not_section
    assert "Google & YouTube" in not_section
    assert "Online Store" not in not_section


def test_get_product_publications_by_handle_uses_handle_query():
    tools, fc = _build([
        _channels_response(),
        {"productByHandle": {
            "id": "gid://shopify/Product/999",
            "title": "Handle tee",
            "handle": "handle-tee",
            "resourcePublications": {"nodes": []},
        }},
    ])
    tools["get_product_publications"](handle="handle-tee")
    assert fc.calls[1][0] == GET_PRODUCT_PUBLICATIONS_BY_HANDLE


def test_get_product_publications_requires_id_or_handle():
    tools, fc = _build([])
    out = tools["get_product_publications"]()
    assert "Provide either product_id or handle" in out


def test_get_product_publications_not_found():
    tools, fc = _build([_channels_response(), {"product": None}])
    out = tools["get_product_publications"](product_id="123")
    assert "No product found" in out


# ---------- publish_product_to_channels ----------

def test_publish_preview_splits_to_publish_vs_unchanged():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        channel_names=["Online Store", "Shop"],
        confirm=False,
    )
    assert "PREVIEW" in out
    would_section = out[out.index("Would publish to"):out.index("Already published")]
    unchanged_section = out[out.index("Already published"):]
    assert "Shop" in would_section
    assert "Online Store" in unchanged_section  # already published → unchanged
    # No mutation in preview
    assert len(fc.calls) == 2


def test_publish_confirmed_only_mutates_needed_channels():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
        _publish_ok(),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        channel_names=["Online Store", "Shop"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    # Only Shop is actually submitted (Online Store already published)
    _, vars_put = fc.calls[2]
    assert vars_put["id"] == "gid://shopify/Product/123"
    assert vars_put["input"] == [{"publicationId": SHOP["id"]}]
    assert fc.calls[2][0] == PUBLISHABLE_PUBLISH


def test_publish_idempotent_all_already_published_skips_mutation():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1, 3], not_published_ids=[2]),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        channel_names=["Online Store", "Shop"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    # No PUBLISHABLE_PUBLISH call — everything already published.
    assert len(fc.calls) == 2
    assert "Unchanged" in out and "Online Store" in out and "Shop" in out


def test_publish_case_insensitive_name_match():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 2]),
        _publish_ok(),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        channel_names=["online store"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    _, vars_put = fc.calls[2]
    assert vars_put["input"] == [{"publicationId": ONLINE["id"]}]


def test_publish_unknown_channel_reported_in_failed_no_mutation_for_it():
    tools, fc = _build([
        _channels_response(),
        _channels_response(),  # refresh on miss
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 2, 3]),
        _publish_ok(),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        channel_names=["Online Store", "TikTok Shop"],
        confirm=True,
    )
    # Online Store is submitted; TikTok Shop goes to failed.
    assert "TikTok Shop" in out and "channel not found" in out
    # Calls: initial list, refresh on miss, product read, publish mutation
    _, vars_put = fc.calls[3]
    assert vars_put["input"] == [{"publicationId": ONLINE["id"]}]


def test_publish_user_errors_surfaced():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1]),
        _publish_err("publicationId", "not authorized"),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        channel_names=["Online Store"],
        confirm=True,
    )
    assert "Failed" in out and "not authorized" in out


def test_publish_requires_channels_or_ids():
    tools, fc = _build([_channels_response()])
    out = tools["publish_product_to_channels"](product_id="123", confirm=True)
    assert "provide channel_names or publication_ids" in out


def test_publish_unknown_publication_id_goes_to_failed_no_mutation():
    """Unknown publication_ids must short-circuit to `failed` like unknown names,
    not be forwarded to Shopify as a mystery publicationId."""
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 2, 3]),
        _publish_ok(),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        publication_ids=[ONLINE["id"], "gid://shopify/Publication/99999"],
        confirm=True,
    )
    # Known id gets published; unknown id lands in failed.
    _, vars_put = fc.calls[2]
    assert vars_put["input"] == [{"publicationId": ONLINE["id"]}]
    assert "99999" in out and "not found" in out


def test_publish_user_error_field_path_maps_to_channel_name():
    """Shopify returns field=['input','0','publicationId'] — map that back to
    the actual channel name from our inputs list, not the raw path."""
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 3]),
        {"publishablePublish": {
            "publishable": None,
            "userErrors": [
                {"field": ["input", "1", "publicationId"], "message": "not authorized"},
            ],
        }},
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        channel_names=["Online Store", "Shop"],
        confirm=True,
    )
    # Index 1 in the submitted inputs = Shop (Online Store is index 0).
    assert "Failed" in out
    assert "Shop" in out[out.index("Failed"):]
    assert "not authorized" in out
    # The raw field path should NOT appear as the channel name.
    assert "input.1.publicationId" not in out


def test_publish_rejects_both_channel_names_and_ids():
    tools, fc = _build([_channels_response()])
    out = tools["publish_product_to_channels"](
        product_id="123",
        channel_names=["Online Store"],
        publication_ids=["gid://shopify/Publication/1"],
        confirm=True,
    )
    assert "not both" in out


# ---------- unpublish_product_from_channels ----------

def test_unpublish_preview_splits_to_unpublish_vs_unchanged():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123",
        channel_names=["Online Store", "Shop"],
        confirm=False,
    )
    assert "PREVIEW" in out
    would_section = out[out.index("Would unpublish from"):out.index("Not currently published")]
    unchanged_section = out[out.index("Not currently published"):]
    assert "Online Store" in would_section
    assert "Shop" in unchanged_section  # never published → unchanged


def test_unpublish_confirmed_only_mutates_currently_published():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
        _unpublish_ok(),
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123",
        channel_names=["Online Store", "Shop"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    _, vars_put = fc.calls[2]
    assert vars_put["input"] == [{"publicationId": ONLINE["id"]}]
    assert fc.calls[2][0] == PUBLISHABLE_UNPUBLISH


def test_unpublish_idempotent_when_already_unpublished():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 2, 3]),
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123",
        channel_names=["Online Store"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert len(fc.calls) == 2  # no mutation
    assert "Unchanged" in out and "Online Store" in out


# ---------- set_product_publications ----------

def test_set_declarative_computes_diff():
    """
    Current = {Online Store, Google}. Desired = {Point of Sale, Google}.
    Expect: add POS, remove Online Store, unchanged Google.
    """
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1, 4], not_published_ids=[2, 3]),
        _publish_ok(),
        _unpublish_ok(),
    ])
    out = tools["set_product_publications"](
        product_id="123",
        channel_names=["Point of Sale", "Google & YouTube"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    added_section = out[out.index("Added (published)"):out.index("Removed (unpublished)")]
    removed_section = out[out.index("Removed (unpublished)"):out.index("Unchanged")]
    unchanged_section = out[out.index("Unchanged"):]
    assert "Point of Sale" in added_section
    assert "Online Store" in removed_section
    assert "Google & YouTube" in unchanged_section

    # Verify mutation payloads: publish POS, unpublish Online Store
    publish_vars = fc.calls[2][1]
    unpublish_vars = fc.calls[3][1]
    assert publish_vars["input"] == [{"publicationId": POS["id"]}]
    assert unpublish_vars["input"] == [{"publicationId": ONLINE["id"]}]


def test_set_declarative_no_ops_when_desired_matches_current():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1, 4], not_published_ids=[2, 3]),
    ])
    out = tools["set_product_publications"](
        product_id="123",
        channel_names=["Online Store", "Google & YouTube"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert len(fc.calls) == 2  # no publish, no unpublish


def test_set_declarative_preview_does_not_mutate():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3, 4]),
    ])
    out = tools["set_product_publications"](
        product_id="123",
        channel_names=["Shop"],
        confirm=False,
    )
    assert "PREVIEW" in out and "confirm=True" in out
    assert len(fc.calls) == 2


def test_set_declarative_requires_channel_names():
    tools, fc = _build([])
    out = tools["set_product_publications"](product_id="123", confirm=True)
    assert "Provide channel_names" in out


# ---------- numeric publication_id normalization ----------
# get_product_publications prints numeric IDs (GID prefix stripped), so users
# copy-pasting those IDs into publish/unpublish must succeed even though the
# cache is keyed by full GID.

def test_publish_accepts_numeric_publication_id():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 2, 3]),
        _publish_ok(),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        publication_ids=["1"],  # bare numeric, as get_product_publications prints
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    # The mutation must receive the full GID, not the bare numeric.
    _, vars_put = fc.calls[2]
    assert vars_put["input"] == [{"publicationId": ONLINE["id"]}]


def test_unpublish_accepts_numeric_publication_id():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
        _unpublish_ok(),
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123",
        publication_ids=["1"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    _, vars_put = fc.calls[2]
    assert vars_put["input"] == [{"publicationId": ONLINE["id"]}]


def test_unpublish_accepts_mixed_gid_and_numeric_ids():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1, 2], not_published_ids=[3, 4]),
        _unpublish_ok(),
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123",
        publication_ids=["1", POS["id"]],  # numeric + full GID in same call
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    _, vars_put = fc.calls[2]
    submitted_ids = {i["publicationId"] for i in vars_put["input"]}
    assert submitted_ids == {ONLINE["id"], POS["id"]}


def test_unknown_numeric_publication_id_reports_raw_input_in_failure():
    """Unknown numeric IDs should still land in `failed` — and the error line
    should show the numeric ID the user passed, not a mangled GID string."""
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 2, 3]),
        _publish_ok(),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123",
        publication_ids=["1", "99999"],
        confirm=True,
    )
    # Known id gets published; unknown numeric id lands in failed with its raw form.
    _, vars_put = fc.calls[2]
    assert vars_put["input"] == [{"publicationId": ONLINE["id"]}]
    assert "99999" in out and "not found" in out
