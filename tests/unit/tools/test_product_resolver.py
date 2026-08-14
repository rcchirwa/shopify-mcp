"""
Offline unit tests for tools/_product_resolver.py.

Story 10.62 (T-9.5-resolver-fanout): the shared numeric/GID/handle dispatch
that six near-twin resolvers across `catalog_hygiene.py` and `publications.py`
each re-implemented now lives in exactly one place — `_resolve_product`. These
tests pin its direct contract (independent of any tool's formatting layer):

  - numeric / GID / handle dispatch, with and without a snapshot query
  - the zero-network-call short-circuit for numeric/GID input when no
    `query_by_id` is supplied (mirrors the old `_resolve_product_gid`)
  - the always-one-call handle path (mirrors the old
    `_resolve_product_with_queries` handle branch)
  - validation failures raise `ValueError` (empty input, wrong-type GID,
    empty GID body) — the shape `_resolve_product_with_queries` already used;
    `catalog_hygiene._resolve_product_gid`'s call-site adapter catches these
    to preserve its historical `(gid, error_str)` return shape, covered in
    test_catalog_hygiene.py instead of here.
  - not-found returns `(None, {})` with no exception, for both the handle
    path and the id/GID path

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_product_resolver.py -v
"""

import pytest

from shopify_mcp.tools._product_resolver import _resolve_product
from tests.support import FakeClient

_GET_BY_ID = "query GetById($id: ID!) { product(id: $id) { id title } }"
_GET_BY_HANDLE = (
    "query GetByHandle($handle: String!) { productByHandle(handle: $handle) { id title } }"
)


def test_numeric_id_without_query_wraps_gid_and_makes_no_network_call():
    fc = FakeClient([])
    gid, snapshot = _resolve_product(fc, "123")
    assert gid == "gid://shopify/Product/123"
    assert snapshot == {}
    assert fc.calls == []


def test_gid_passthrough_without_query_makes_no_network_call():
    fc = FakeClient([])
    gid, snapshot = _resolve_product(fc, "gid://shopify/Product/456")
    assert gid == "gid://shopify/Product/456"
    assert snapshot == {}
    assert fc.calls == []


def test_numeric_id_with_query_fetches_snapshot():
    fc = FakeClient([{"product": {"id": "gid://shopify/Product/123", "title": "Tee"}}])
    gid, snapshot = _resolve_product(fc, "123", query_by_id=_GET_BY_ID)
    assert gid == "gid://shopify/Product/123"
    assert snapshot == {"id": "gid://shopify/Product/123", "title": "Tee"}
    assert fc.calls == [(_GET_BY_ID, {"id": "gid://shopify/Product/123"})]


def test_gid_with_query_fetches_snapshot_and_falls_back_to_input_gid():
    # Defensive fallback: if the returned node omits `id` (shouldn't happen on
    # a real schema), the already-known input GID is used rather than None.
    fc = FakeClient([{"product": {"title": "Tee"}}])
    gid, snapshot = _resolve_product(fc, "gid://shopify/Product/456", query_by_id=_GET_BY_ID)
    assert gid == "gid://shopify/Product/456"
    assert snapshot == {"title": "Tee"}


def test_numeric_id_with_query_not_found_returns_none_and_empty_snapshot():
    fc = FakeClient([{"product": None}])
    gid, snapshot = _resolve_product(fc, "123", query_by_id=_GET_BY_ID)
    assert gid is None
    assert snapshot == {}


def test_handle_without_query_uses_minimal_read():
    fc = FakeClient([{"productByHandle": {"id": "gid://shopify/Product/789"}}])
    gid, snapshot = _resolve_product(fc, "my-handle")
    assert gid == "gid://shopify/Product/789"
    assert snapshot == {"id": "gid://shopify/Product/789"}
    assert len(fc.calls) == 1
    assert fc.calls[0][1] == {"handle": "my-handle"}


def test_handle_with_query_fetches_snapshot():
    fc = FakeClient([{"productByHandle": {"id": "gid://shopify/Product/789", "title": "Tee"}}])
    gid, snapshot = _resolve_product(fc, "my-handle", query_by_handle=_GET_BY_HANDLE)
    assert gid == "gid://shopify/Product/789"
    assert snapshot == {"id": "gid://shopify/Product/789", "title": "Tee"}
    assert fc.calls == [(_GET_BY_HANDLE, {"handle": "my-handle"})]


def test_handle_not_found_returns_none_and_empty_snapshot_no_exception():
    fc = FakeClient([{"productByHandle": None}])
    gid, snapshot = _resolve_product(fc, "ghost-handle", query_by_handle=_GET_BY_HANDLE)
    assert gid is None
    assert snapshot == {}


def test_empty_product_id_raises_value_error_without_network():
    fc = FakeClient([])
    with pytest.raises(ValueError, match="product_id must be a non-empty string"):
        _resolve_product(fc, "")
    assert fc.calls == []


def test_whitespace_only_product_id_raises_value_error():
    fc = FakeClient([])
    with pytest.raises(ValueError, match="product_id must be a non-empty string"):
        _resolve_product(fc, "   ")


def test_non_string_product_id_raises_value_error():
    fc = FakeClient([])
    with pytest.raises(ValueError, match="product_id must be a non-empty string"):
        _resolve_product(fc, None)  # type: ignore[arg-type]


def test_wrong_type_gid_raises_value_error_without_network():
    fc = FakeClient([])
    with pytest.raises(ValueError, match="non-Product GID"):
        _resolve_product(fc, "gid://shopify/Order/1")
    assert fc.calls == []


def test_empty_product_gid_body_raises_value_error():
    fc = FakeClient([])
    with pytest.raises(ValueError, match="Empty product GID body"):
        _resolve_product(fc, "gid://shopify/Product/")
    assert fc.calls == []


def test_product_id_is_stripped_before_dispatch():
    fc = FakeClient([])
    gid, snapshot = _resolve_product(fc, "  123  ")
    assert gid == "gid://shopify/Product/123"
    assert snapshot == {}
