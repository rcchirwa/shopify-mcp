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

import pytest

from gql.transport.exceptions import TransportQueryError, TransportServerError

from shopify_client import ShopifyClient, _format_errors, _mask_token, from_gid


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


def test_format_errors_handles_non_list_non_str_shape():
    # gql occasionally surfaces errors as a single dict or other scalar
    # instead of a list — the formatter must stringify rather than crash.
    assert _format_errors({"message": "solo"}) == "{'message': 'solo'}"
    assert _format_errors(42) == "42"


# ---------- _mask_token helper ----------

def test_mask_token_preserves_shpat_prefix_and_last4():
    # Full-length Shopify admin token: shpat_ + 32 hex = 38 chars.
    token = "shpat_" + "0" * 28 + "abcd"
    masked = _mask_token(token)
    assert masked == "shpat_…abcd"
    assert "0" * 28 not in masked, "body of token must not leak"


def test_mask_token_non_shpat_uses_first4_last4():
    token = "abcdef1234567890XYZW"
    masked = _mask_token(token)
    assert masked == "abcd…XYZW"
    assert "ef1234567890XYZ" not in masked


def test_mask_token_short_token_fully_masked():
    # Tokens shorter than 9 chars: mask entirely (nothing safe to leak).
    assert _mask_token("abc") == "***"
    assert _mask_token("abcdefgh") == "********"


def test_mask_token_empty_or_none():
    assert _mask_token("") == "(empty)"
    assert _mask_token(None) == "(empty)"


# ---------- from_gid helper ----------

def test_from_gid_extracts_trailing_numeric_id():
    assert from_gid("gid://shopify/Product/123") == "123"


def test_from_gid_tolerates_none():
    # Shopify can return `id: null` on partial / permissions-trimmed fields,
    # and callers that do `from_gid(obj.get("id", ""))` still pass None through
    # because .get only applies the default for missing keys. Must not crash.
    assert from_gid(None) == ""


def test_from_gid_tolerates_empty_string():
    assert from_gid("") == ""


# ---------- .env loading: override=True + script-relative path ----------

def test_init_env_override_wins_over_process_env(tmp_path, monkeypatch, capsys):
    """.env on disk must win over stale env vars injected by the launcher."""
    import shopify_client as sc

    # Simulate Claude-Desktop-style injection: process env has the OLD token.
    monkeypatch.setenv("SHOPIFY_STORE_URL", "stale.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_stale00000000000000000000old1")
    monkeypatch.setenv("SHOPIFY_API_VERSION", "2023-01")

    # And .env on disk has the NEW token.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SHOPIFY_STORE_URL=fresh.myshopify.com\n"
        "SHOPIFY_ACCESS_TOKEN=shpat_fresh000000000000000000new2\n"
        "SHOPIFY_API_VERSION=2024-10\n"
    )
    monkeypatch.setattr(sc, "_ENV_PATH", env_file)

    # Avoid real HTTP client construction — replace Client with a stub.
    monkeypatch.setattr(sc, "Client", lambda **_kw: object())
    monkeypatch.setattr(sc, "RequestsHTTPTransport", lambda **_kw: object())

    sc.ShopifyClient()

    # After __init__, os.environ should reflect .env values (override=True).
    assert os.environ["SHOPIFY_STORE_URL"] == "fresh.myshopify.com"
    assert os.environ["SHOPIFY_ACCESS_TOKEN"].endswith("new2")
    assert os.environ["SHOPIFY_API_VERSION"] == "2024-10"

    # Startup fingerprint log goes to stderr, masked, with .env source.
    err = capsys.readouterr().err
    assert "store=fresh.myshopify.com" in err
    assert "api_version=2024-10" in err
    assert "token=shpat_…new2" in err
    assert "source=.env" in err
    # The old stale token must not appear anywhere in the log.
    assert "old1" not in err
    assert "stale" not in err


def test_init_missing_credentials_raises(monkeypatch, tmp_path):
    """No .env on disk + no process env → clear ValueError."""
    import shopify_client as sc

    monkeypatch.delenv("SHOPIFY_STORE_URL", raising=False)
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(sc, "_ENV_PATH", tmp_path / "nonexistent.env")

    with pytest.raises(ValueError, match="SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN"):
        sc.ShopifyClient()
