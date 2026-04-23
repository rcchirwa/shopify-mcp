"""
Offline unit tests for tools/publications.py.

Uses a scripted FakeClient to exercise name resolution, preview/confirm,
idempotency, and the declarative set_product_publications diff.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_publications_offline.py -v
"""

from tools import publications
from tools.publications import (
    LIST_PUBLICATIONS,
    GET_PRODUCT_PUBLICATIONS_BY_ID,
    GET_PRODUCT_PUBLICATIONS_BY_HANDLE,
    PUBLISHABLE_PUBLISH,
    PUBLISHABLE_UNPUBLISH,
    _map_user_error,
    _split_current,
    _resolve_product_gid_and_meta,
)
from _testing import CapturingServer, FakeClient


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


# ---------- _map_user_error direct coverage ----------

def test_map_user_error_non_integer_index_falls_back_to_raw_path():
    """Shopify spec says field=['input', <idx>, <key>]. A future shape with a
    non-numeric index (or a bare scalar like 'input') must not crash — fall
    back to the joined raw path so the operator still sees the error."""
    targets = [{"id": "gid://shopify/Publication/1", "name": "Online Store"}]
    mapped = _map_user_error(
        {"field": ["input", "NaN", "publicationId"], "message": "boom"},
        targets,
    )
    assert mapped["channel_name"] == "input.NaN.publicationId"
    assert mapped["error"] == "boom"


def test_map_user_error_non_list_field_falls_back_to_stringified():
    mapped = _map_user_error({"field": "root", "message": "boom"}, [])
    assert mapped["channel_name"] == "root"


def test_map_user_error_empty_field_falls_back_to_unknown():
    mapped = _map_user_error({"field": [], "message": "boom"}, [])
    assert mapped["channel_name"] == "(unknown)"


# ---------- _split_current: rp with missing publication id ----------

def test_split_current_skips_resourcepublications_without_publication_id():
    """A resourcePublication with no publication id can't be acted on — skip
    it instead of inserting a garbage entry into the published set."""
    rps = [
        {"publication": {"id": "gid://shopify/Publication/1"}, "isPublished": True},
        {"publication": {}, "isPublished": True},          # missing id
        {"publication": None, "isPublished": False},       # missing publication entirely
    ]
    pub, not_pub = _split_current(rps)
    assert pub == {"gid://shopify/Publication/1"}
    assert not_pub == set()


# ---------- _resolve_product_gid_and_meta direct: neither id nor handle ----------

def test_resolve_product_meta_returns_all_none_when_neither_id_nor_handle():
    """Public tools guard against this upstream, but the helper itself is
    the single source of truth for 'no identifier' → all-None. Asserted
    directly since no public path exercises it."""
    # Client will never be called because neither branch is taken.
    fc = FakeClient([])
    result = _resolve_product_gid_and_meta(fc, "", "")
    assert result == (None, None, None, None)
    assert fc.calls == []


# ---------- get_product_publications: exception paths ----------

def test_get_product_publications_channel_load_failure_surfaces_hint():
    fc = FakeClient([RuntimeError("Access denied for publications")])
    srv = CapturingServer()
    publications.register(srv, fc)
    out = srv.tools["get_product_publications"](product_id="123")
    assert "Error loading sales channels" in out
    assert "Access denied" in out
    assert "read_publications" in out


def test_get_product_publications_product_read_failure_surfaces_hint():
    tools, fc = _build([
        _channels_response(),
        RuntimeError("Shopify GraphQL error: transient"),
    ])
    out = tools["get_product_publications"](product_id="123")
    assert out.startswith("Error:")
    assert "transient" in out
    assert "read_publications" in out


# ---------- publish_product_to_channels: missing identifier + exception paths ----------

def test_publish_requires_product_id_or_handle():
    tools, fc = _build([])
    out = tools["publish_product_to_channels"](
        channel_names=["Online Store"], confirm=True,
    )
    assert out == "Provide either product_id or handle."
    assert fc.calls == []


def test_publish_channel_resolve_exception_surfaces_hint():
    fc = FakeClient([RuntimeError("Access denied for publications")])
    srv = CapturingServer()
    publications.register(srv, fc)
    out = srv.tools["publish_product_to_channels"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert "Error resolving channels" in out
    assert "read_publications" in out


def test_publish_product_read_exception_surfaces_hint():
    tools, fc = _build([
        _channels_response(),
        RuntimeError("transient network"),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert out.startswith("Error:")
    assert "transient network" in out


def test_publish_product_not_found_reports_clean_error():
    tools, fc = _build([
        _channels_response(),
        {"product": None},
    ])
    out = tools["publish_product_to_channels"](
        product_id="nope", channel_names=["Online Store"], confirm=True,
    )
    assert out == "No product found."


def test_publish_mutation_exception_surfaces_hint():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 2, 3]),
        RuntimeError("502 Bad Gateway"),
    ])
    out = tools["publish_product_to_channels"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert out.startswith("Error:")
    assert "502 Bad Gateway" in out


# ---------- unpublish_product_from_channels: gap branches ----------

def test_unpublish_requires_product_id_or_handle():
    tools, fc = _build([])
    out = tools["unpublish_product_from_channels"](
        channel_names=["Online Store"], confirm=True,
    )
    assert out == "Provide either product_id or handle."
    assert fc.calls == []


def test_unpublish_channel_resolve_exception_surfaces_hint():
    fc = FakeClient([RuntimeError("Access denied for publications")])
    srv = CapturingServer()
    publications.register(srv, fc)
    out = srv.tools["unpublish_product_from_channels"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert "Error resolving channels" in out
    assert "read_publications" in out


def test_unpublish_rejects_both_channel_names_and_ids():
    tools, fc = _build([_channels_response()])
    out = tools["unpublish_product_from_channels"](
        product_id="123",
        channel_names=["Online Store"],
        publication_ids=["gid://shopify/Publication/1"],
        confirm=True,
    )
    assert "not both" in out


def test_unpublish_product_read_exception_surfaces_hint():
    tools, fc = _build([
        _channels_response(),
        RuntimeError("transient network"),
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert out.startswith("Error:")
    assert "transient network" in out


def test_unpublish_product_not_found_reports_clean_error():
    tools, fc = _build([
        _channels_response(),
        {"product": None},
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="nope", channel_names=["Online Store"], confirm=True,
    )
    assert out == "No product found."


def test_unpublish_preview_shows_failed_to_resolve_block_when_channel_unknown():
    """Unknown channel name → preview must surface it under 'Failed to resolve'
    instead of silently dropping it."""
    tools, fc = _build([
        _channels_response(),
        _channels_response(),  # refresh on miss
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123",
        channel_names=["Online Store", "TikTok Shop"],
        confirm=False,
    )
    assert "PREVIEW" in out
    assert "Failed to resolve" in out
    assert "TikTok Shop" in out


def test_unpublish_mutation_exception_surfaces_hint():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
        RuntimeError("502 Bad Gateway"),
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert out.startswith("Error:")
    assert "502 Bad Gateway" in out


def test_unpublish_user_errors_surface_in_confirmed_body():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
        {"publishableUnpublish": {
            "publishable": None,
            "userErrors": [
                {"field": ["input", "0", "publicationId"], "message": "not authorized"},
            ],
        }},
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Failed" in out
    assert "Online Store" in out[out.index("Failed"):]
    assert "not authorized" in out


def test_unpublish_confirmed_failed_block_rendered_when_unknown_channel():
    """Unknown channel resolved at preview must carry through to CONFIRMED
    output — the caller needs to know which channels didn't get touched."""
    tools, fc = _build([
        _channels_response(),
        _channels_response(),  # refresh on miss
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
        _unpublish_ok(),
    ])
    out = tools["unpublish_product_from_channels"](
        product_id="123",
        channel_names=["Online Store", "TikTok Shop"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Failed" in out
    assert "TikTok Shop" in out[out.index("Failed"):]


# ---------- set_product_publications: gap branches ----------

def test_set_requires_product_id_or_handle():
    tools, fc = _build([])
    out = tools["set_product_publications"](
        channel_names=["Online Store"], confirm=True,
    )
    assert out == "Provide either product_id or handle."
    assert fc.calls == []


def test_set_channel_resolve_exception_surfaces_hint():
    fc = FakeClient([RuntimeError("Access denied for publications")])
    srv = CapturingServer()
    publications.register(srv, fc)
    out = srv.tools["set_product_publications"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert "Error resolving channels" in out
    assert "read_publications" in out


def test_set_product_read_exception_surfaces_hint():
    tools, fc = _build([
        _channels_response(),
        RuntimeError("transient network"),
    ])
    out = tools["set_product_publications"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert out.startswith("Error:")
    assert "transient network" in out


def test_set_product_not_found_reports_clean_error():
    tools, fc = _build([
        _channels_response(),
        {"product": None},
    ])
    out = tools["set_product_publications"](
        product_id="nope", channel_names=["Online Store"], confirm=True,
    )
    assert out == "No product found."


def test_set_preview_surfaces_failed_to_resolve_for_unknown_channels():
    tools, fc = _build([
        _channels_response(),
        _channels_response(),  # refresh on miss
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3, 4]),
    ])
    out = tools["set_product_publications"](
        product_id="123",
        channel_names=["Online Store", "TikTok Shop"],
        confirm=False,
    )
    assert "PREVIEW" in out
    assert "Failed to resolve" in out
    assert "TikTok Shop" in out


def test_set_publish_mutation_exception_surfaces_publish_specific_hint():
    """Exceptions during the publish mutation must name the phase ('publish')
    so the operator knows whether any unpublish ran after it."""
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 2, 3]),
        RuntimeError("publish 502"),
    ])
    out = tools["set_product_publications"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert "Error during publish" in out
    assert "publish 502" in out


def test_set_publish_user_errors_carry_into_apply_failed():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[], not_published_ids=[1, 2, 3]),
        _publish_err("publicationId", "not authorized"),
    ])
    out = tools["set_product_publications"](
        product_id="123", channel_names=["Online Store"], confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Failed" in out
    assert "not authorized" in out


def test_set_unpublish_mutation_exception_surfaces_unpublish_specific_hint():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1, 4], not_published_ids=[2, 3]),
        _publish_ok(),                 # add (publish) succeeds
        RuntimeError("unpublish 502"), # remove (unpublish) fails
    ])
    out = tools["set_product_publications"](
        product_id="123",
        channel_names=["Point of Sale", "Google & YouTube"],
        confirm=True,
    )
    assert "Error during unpublish" in out
    assert "unpublish 502" in out


def test_set_unpublish_user_errors_carry_into_apply_failed():
    tools, fc = _build([
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1, 4], not_published_ids=[2, 3]),
        _publish_ok(),
        {"publishableUnpublish": {
            "publishable": None,
            "userErrors": [
                {"field": ["input", "0", "publicationId"], "message": "locked"},
            ],
        }},
    ])
    out = tools["set_product_publications"](
        product_id="123",
        channel_names=["Point of Sale", "Google & YouTube"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Failed" in out
    assert "locked" in out


def test_set_confirmed_body_renders_failed_block_for_unknown_channel():
    """set_product_publications must carry resolve-time failures through to
    the CONFIRMED body, not just the preview."""
    tools, fc = _build([
        _channels_response(),
        _channels_response(),  # refresh on miss
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
        _publish_ok(),
    ])
    out = tools["set_product_publications"](
        product_id="123",
        channel_names=["Online Store", "Shop", "TikTok Shop"],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert "Failed" in out
    assert "TikTok Shop" in out[out.index("Failed"):]
