"""
Offline unit tests for shopify_client.ShopifyClient.execute() boundary.

Exercises the non-dict response check and the TransportQueryError formatting
path without hitting Shopify or requiring .env credentials.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_shopify_client_offline.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from gql.transport.exceptions import TransportQueryError, TransportServerError

from shopify_client import ShopifyClient, _format_errors


class _StubGqlClient:
    """Stand-in for gql.Client — returns a scripted value or raises."""
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def execute(self, *_args, **_kwargs):
        if self._exc is not None:
            raise self._exc
        return self._result


def _make_client(result=None, exc=None):
    """Build a ShopifyClient without invoking __init__ (skips .env load)."""
    client = object.__new__(ShopifyClient)
    client._client = _StubGqlClient(result=result, exc=exc)
    return client


# ---------- normal dict response passes through ----------

def test_execute_returns_dict_unchanged():
    client = _make_client(result={"products": {"nodes": []}})
    assert client.execute("query { __typename }") == {"products": {"nodes": []}}


# ---------- non-dict responses raise clear RuntimeError ----------

def test_execute_raises_clear_error_on_string_response():
    client = _make_client(result="Unauthorized: missing read_products scope")
    with pytest.raises(RuntimeError) as exc_info:
        client.execute("query { __typename }")
    msg = str(exc_info.value)
    assert "non-dict response" in msg
    assert "type=str" in msg
    assert "Unauthorized" in msg


def test_execute_raises_clear_error_on_none_response():
    client = _make_client(result=None)
    with pytest.raises(RuntimeError, match="non-dict response.*type=NoneType"):
        client.execute("query { __typename }")


def test_execute_raises_clear_error_on_list_response():
    client = _make_client(result=[{"unexpected": "shape"}])
    with pytest.raises(RuntimeError, match="non-dict response.*type=list"):
        client.execute("query { __typename }")


def test_execute_truncates_large_non_dict_preview():
    huge = "x" * 10_000
    client = _make_client(result=huge)
    with pytest.raises(RuntimeError) as exc_info:
        client.execute("query { __typename }")
    msg = str(exc_info.value)
    # Preview is capped at 500 chars — full 10k payload must not appear.
    assert len(msg) < 1000
    assert "x" * 500 in msg


# ---------- transport exception path still works (regression for 98c9bed) ----------

def test_execute_formats_transport_query_error_with_string_errors():
    err = TransportQueryError("boom", errors="raw string error body")
    client = _make_client(exc=err)
    with pytest.raises(RuntimeError, match="Shopify GraphQL error: raw string error body"):
        client.execute("query { __typename }")


def test_execute_formats_transport_query_error_with_dict_errors():
    err = TransportQueryError("boom", errors=[{"message": "Field 'x' doesn't exist"}])
    client = _make_client(exc=err)
    with pytest.raises(RuntimeError, match="Field 'x' doesn't exist"):
        client.execute("query { __typename }")


def test_execute_wraps_transport_server_error():
    err = TransportServerError("503 Service Unavailable")
    client = _make_client(exc=err)
    with pytest.raises(RuntimeError, match="Shopify HTTP error:.*503"):
        client.execute("query { __typename }")


# ---------- _format_errors helper shapes ----------

def test_format_errors_handles_none():
    assert _format_errors(None) == "(no error details)"


def test_format_errors_handles_string():
    assert _format_errors("bare string") == "bare string"


def test_format_errors_joins_mixed_list():
    errors = [{"message": "first"}, "second", 42]
    assert _format_errors(errors) == "first; second; 42"
