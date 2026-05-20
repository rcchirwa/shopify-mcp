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

import shopify_client as sc
from shopify_client import (
    ShopifyClient,
    ShopifyError,
    TransientShopifyError,
    _backoff_delay,
    _format_errors,
    _is_retryable_http,
    _is_throttled,
    _mask_token,
    extract_user_errors,
    format_user_errors,
    format_user_errors_joined,
    from_gid,
    with_confirm_hint,
)


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
    with pytest.raises(RuntimeError, match=r"non-dict response.*type=NoneType"):
        client.execute("query { __typename }")


def test_execute_raises_clear_error_on_list_response():
    client = _make_client(result=[{"unexpected": "shape"}])
    with pytest.raises(RuntimeError, match=r"non-dict response.*type=list"):
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
    # 400 is a permanent error (not retryable) — raises immediately as ShopifyError.
    err = TransportServerError("400 Bad Request")
    client = _make_client(exc=err)
    with pytest.raises(ShopifyError, match=r"Shopify HTTP error:.*400"):
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


# ---------- with_confirm_hint helper ----------


def test_with_confirm_hint_appends_exact_contract_string():
    # The tail is asserted verbatim by tool-level tests (test_inventory_offline,
    # test_discounts_offline). Pin it here too so drift is caught at the source.
    assert with_confirm_hint("PREVIEW — Something") == (
        "PREVIEW — Something\n\nTo apply, call again with confirm=True."
    )


def test_with_confirm_hint_on_empty_preview():
    assert with_confirm_hint("") == "\n\nTo apply, call again with confirm=True."


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


# ---------------------------------------------------------------------------
# format_user_errors
# ---------------------------------------------------------------------------


def test_format_user_errors_happy_path():
    result = {
        "productUpdate": {
            "userErrors": [
                {"field": "title", "message": "can't be blank"},
                {"field": "handle", "message": "already taken"},
            ]
        }
    }
    assert format_user_errors(result, "productUpdate") == (
        "Error: title: can't be blank; handle: already taken"
    )


def test_format_user_errors_no_errors_returns_none():
    result = {"productUpdate": {"userErrors": []}}
    assert format_user_errors(result, "productUpdate") is None


def test_format_user_errors_missing_mutation_key_returns_none():
    # Whole mutation slot absent (e.g. partial/permissions-trimmed response).
    assert format_user_errors({}, "productUpdate") is None


def test_format_user_errors_missing_user_errors_slot_returns_none():
    # Mutation returned, but the userErrors key was omitted entirely.
    assert format_user_errors({"productUpdate": {}}, "productUpdate") is None


def test_format_user_errors_mutation_slot_is_none_returns_none():
    # GraphQL can return explicit null for a mutation payload — `or {}` guard.
    assert format_user_errors({"productUpdate": None}, "productUpdate") is None


def test_format_user_errors_alt_error_key():
    # priceRuleCreate uses priceRuleUserErrors instead of userErrors.
    result = {
        "priceRuleCreate": {"priceRuleUserErrors": [{"field": "value", "message": "out of range"}]}
    }
    assert (
        format_user_errors(result, "priceRuleCreate", error_key="priceRuleUserErrors")
        == "Error: value: out of range"
    )


def test_format_user_errors_custom_prefix():
    result = {
        "priceRuleDiscountCodeCreate": {
            "userErrors": [{"field": "code", "message": "already exists"}]
        }
    }
    assert (
        format_user_errors(
            result, "priceRuleDiscountCodeCreate", prefix="Error attaching discount code"
        )
        == "Error attaching discount code: code: already exists"
    )


def test_format_user_errors_tolerates_missing_field_or_message():
    # Defensive: Shopify's contract guarantees both keys, but an unexpected
    # response shape yields "None: None" rather than a KeyError.
    result = {"productUpdate": {"userErrors": [{}]}}
    assert format_user_errors(result, "productUpdate") == "Error: None: None"


# ---------------------------------------------------------------------------
# format_user_errors_joined
# ---------------------------------------------------------------------------


def test_format_user_errors_joined_happy_path():
    # Same payload as format_user_errors — but no "Error: " prefix. Used by
    # bulk-op summaries that embed the formatted string inside a bullet row.
    result = {
        "productUpdate": {
            "userErrors": [
                {"field": "title", "message": "can't be blank"},
                {"field": "handle", "message": "already taken"},
            ]
        }
    }
    assert format_user_errors_joined(result, "productUpdate") == (
        "title: can't be blank; handle: already taken"
    )


def test_format_user_errors_joined_no_errors_returns_none():
    assert format_user_errors_joined({"productUpdate": {"userErrors": []}}, "productUpdate") is None


def test_format_user_errors_joined_missing_mutation_key_returns_none():
    assert format_user_errors_joined({}, "productUpdate") is None


def test_format_user_errors_joined_missing_user_errors_slot_returns_none():
    # Mutation returned, but the userErrors key was omitted entirely.
    assert format_user_errors_joined({"productUpdate": {}}, "productUpdate") is None


def test_format_user_errors_joined_mutation_slot_is_none_returns_none():
    assert format_user_errors_joined({"productUpdate": None}, "productUpdate") is None


def test_format_user_errors_joined_alt_error_key():
    result = {
        "priceRuleCreate": {"priceRuleUserErrors": [{"field": "value", "message": "out of range"}]}
    }
    assert (
        format_user_errors_joined(result, "priceRuleCreate", error_key="priceRuleUserErrors")
        == "value: out of range"
    )


def test_format_user_errors_joined_tolerates_missing_field_or_message():
    result: dict = {"productUpdate": {"userErrors": [{}]}}
    assert format_user_errors_joined(result, "productUpdate") == "None: None"


# ---------------------------------------------------------------------------
# extract_user_errors
# ---------------------------------------------------------------------------


def test_extract_user_errors_returns_list():
    errors = [{"field": "title", "message": "blank"}]
    result = {"productUpdate": {"userErrors": errors}}
    assert extract_user_errors(result, "productUpdate") == errors


def test_extract_user_errors_missing_mutation_returns_empty_list():
    assert extract_user_errors({}, "productUpdate") == []


def test_extract_user_errors_null_mutation_returns_empty_list():
    # Shopify can return explicit null for a mutation payload.
    assert extract_user_errors({"productUpdate": None}, "productUpdate") == []


def test_extract_user_errors_null_list_returns_empty_list():
    # Mutation returned, userErrors slot is explicit null rather than [].
    assert extract_user_errors({"productUpdate": {"userErrors": None}}, "productUpdate") == []


def test_extract_user_errors_alt_error_key():
    result = {"publishablePublish": {"mediaUserErrors": [{"field": "media", "message": "bad"}]}}
    assert extract_user_errors(result, "publishablePublish", error_key="mediaUserErrors") == [
        {"field": "media", "message": "bad"}
    ]


# ===========================================================================
# Scripted stub + fixtures for retry/backoff tests
# ===========================================================================


class _ScriptedGqlClient:
    """Stub gql.Client that walks a script of (result | exception) per execute()."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def execute(self, *_args, **_kwargs):
        self.calls += 1
        if not self._script:
            raise AssertionError(
                f"_ScriptedGqlClient script exhausted after {self.calls - 1} calls"
            )
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _make_scripted(script):
    """Build a ShopifyClient backed by a scripted stub, without invoking __init__."""
    client = object.__new__(ShopifyClient)
    client._client = _ScriptedGqlClient(script)
    return client


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace time.sleep in shopify_client with a recorder (no actual sleeping)."""
    sleeps: list[float] = []
    monkeypatch.setattr(sc.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


@pytest.fixture
def deterministic_jitter(monkeypatch):
    """Pin random.uniform(lo, hi) → hi so backoff durations are predictable."""
    monkeypatch.setattr(sc.random, "uniform", lambda _lo, hi: hi)


# ===========================================================================
# _is_throttled helper
# ===========================================================================


def test_is_throttled_dict_with_extensions_code():
    assert _is_throttled([{"extensions": {"code": "THROTTLED"}}]) is True


def test_is_throttled_dict_with_message_substring():
    assert _is_throttled([{"message": "Request was THROTTLED by cost"}]) is True


def test_is_throttled_bare_string():
    assert _is_throttled("THROTTLED: bucket empty") is True


def test_is_throttled_string_in_list():
    assert _is_throttled(["THROTTLED"]) is True


def test_is_throttled_none_returns_false():
    assert _is_throttled(None) is False


def test_is_throttled_unrelated_error_false():
    assert _is_throttled([{"message": "Field 'x' doesn't exist"}]) is False


def test_is_throttled_non_list_non_str_shape():
    # A dict (not a list) — falls through to str() check.
    assert _is_throttled({"extensions": {"code": "THROTTLED"}}) is True


def test_is_throttled_list_with_non_dict_non_str_item():
    # A list containing an object that is neither dict nor str but whose
    # str() representation contains "THROTTLED" — hits the final elif branch.
    class _FakeGraphQLError:
        def __str__(self):
            return "GraphQLError: THROTTLED by cost bucket"

    assert _is_throttled([_FakeGraphQLError()]) is True


# ===========================================================================
# _is_retryable_http helper
# ===========================================================================


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_is_retryable_http_retryable_statuses(status):
    assert _is_retryable_http(TransportServerError(f"{status} Server Error")) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_is_retryable_http_non_retryable_statuses(status):
    assert _is_retryable_http(TransportServerError(f"{status} Client Error")) is False


def test_is_retryable_http_no_false_positive_on_embedded_digits():
    # A 404 error whose body happens to contain "/v500/" in a URL path must
    # NOT be treated as retryable — word-boundary regex prevents the match.
    msg = "404 Not Found for url: https://api.myshopify.com/admin/api/v500/graphql.json"
    assert _is_retryable_http(TransportServerError(msg)) is False


# ===========================================================================
# _backoff_delay helper
# ===========================================================================


def test_backoff_delay_jitter_true_within_ceiling(monkeypatch):
    # With real random, the delay must be in [0, ceiling] for every attempt.
    for attempt in range(6):
        ceiling = min(30.0, 0.5 * (2**attempt))
        delay = _backoff_delay(attempt, base=0.5, cap=30.0, jitter=True)
        assert 0.0 <= delay <= ceiling, f"attempt={attempt}: {delay} not in [0, {ceiling}]"


def test_backoff_delay_jitter_false_returns_ceiling():
    # jitter=False must return exactly min(cap, base * 2^attempt).
    assert _backoff_delay(0, base=0.5, cap=30.0, jitter=False) == 0.5
    assert _backoff_delay(1, base=0.5, cap=30.0, jitter=False) == 1.0
    assert _backoff_delay(10, base=0.5, cap=30.0, jitter=False) == 30.0  # capped


def test_backoff_delay_cap_enforced():
    assert _backoff_delay(100, base=0.5, cap=5.0, jitter=False) == 5.0


# ===========================================================================
# Exception hierarchy
# ===========================================================================


def test_shopify_error_is_runtime_error():
    assert issubclass(ShopifyError, RuntimeError)


def test_transient_shopify_error_is_runtime_error():
    assert issubclass(TransientShopifyError, RuntimeError)


# ===========================================================================
# execute() retry logic
# ===========================================================================


def test_execute_retries_throttled_dict_error_then_succeeds(no_sleep):
    throttled_err = TransportQueryError(
        "throttled", errors=[{"extensions": {"code": "THROTTLED"}, "message": "Throttled"}]
    )
    client = _make_scripted([throttled_err, {"products": {}}])
    result = client.execute("{ products { nodes { id } } }")
    assert result == {"products": {}}
    assert len(no_sleep) == 1


def test_execute_retries_throttled_string_in_message_then_succeeds(no_sleep):
    throttled_err = TransportQueryError("t", errors=[{"message": "THROTTLED by cost bucket"}])
    client = _make_scripted([throttled_err, {"ok": 1}])
    assert client.execute("{ __typename }") == {"ok": 1}
    assert len(no_sleep) == 1


def test_execute_retries_throttled_bare_string_errors(no_sleep):
    throttled_err = TransportQueryError("t", errors="THROTTLED")
    client = _make_scripted([throttled_err, {"ok": 1}])
    assert client.execute("{ __typename }") == {"ok": 1}
    assert len(no_sleep) == 1


def test_execute_retries_on_429_then_succeeds(no_sleep):
    err = TransportServerError("429 Too Many Requests")
    client = _make_scripted([err, {"ok": 1}])
    assert client.execute("{ __typename }") == {"ok": 1}
    assert len(no_sleep) == 1


def test_execute_retries_on_503_then_succeeds(no_sleep):
    err = TransportServerError("503 Service Unavailable")
    client = _make_scripted([err, {"ok": 1}])
    assert client.execute("{ __typename }") == {"ok": 1}
    assert len(no_sleep) == 1


@pytest.mark.parametrize("status", [500, 502, 504])
def test_execute_retries_on_5xx_statuses(status, no_sleep):
    err = TransportServerError(f"{status} Server Error")
    client = _make_scripted([err, {"ok": 1}])
    assert client.execute("{ __typename }") == {"ok": 1}
    assert len(no_sleep) == 1


def test_execute_does_not_retry_on_400(no_sleep):
    err = TransportServerError("400 Bad Request")
    client = _make_scripted([err])
    with pytest.raises(ShopifyError, match="Shopify HTTP error: 400 Bad Request"):
        client.execute("{ __typename }")
    assert no_sleep == []
    assert client._client.calls == 1


def test_execute_does_not_retry_on_non_throttle_gql_error(no_sleep):
    err = TransportQueryError("schema", errors=[{"message": "Unknown field 'foo'"}])
    client = _make_scripted([err])
    with pytest.raises(ShopifyError, match="Shopify GraphQL error: Unknown field 'foo'"):
        client.execute("{ __typename }")
    assert no_sleep == []
    assert client._client.calls == 1


def test_execute_exhausts_retries_on_persistent_throttled(no_sleep):
    throttled_err = TransportQueryError(
        "t", errors=[{"extensions": {"code": "THROTTLED"}, "message": "Throttled"}]
    )
    # 6 total attempts = initial + 5 retries
    client = _make_scripted([throttled_err] * 6)
    with pytest.raises(TransientShopifyError) as exc_info:
        client.execute("{ __typename }")
    assert "after 6 attempts" in str(exc_info.value)
    assert len(no_sleep) == 5
    assert client._client.calls == 6


def test_execute_exhausts_retries_on_persistent_503(no_sleep):
    err = TransportServerError("503 Service Unavailable")
    client = _make_scripted([err] * 6)
    with pytest.raises(TransientShopifyError) as exc_info:
        client.execute("{ __typename }")
    assert "after 6 attempts" in str(exc_info.value)
    assert len(no_sleep) == 5
    assert client._client.calls == 6


def test_execute_backoff_schedule_exponential(no_sleep, deterministic_jitter):
    # With jitter pinned to ceiling, sleeps follow min(30, 0.5 * 2^attempt).
    throttled_err = TransportQueryError(
        "t", errors=[{"extensions": {"code": "THROTTLED"}, "message": "Throttled"}]
    )
    client = _make_scripted([throttled_err] * 6)
    with pytest.raises(TransientShopifyError):
        client.execute("{ __typename }")
    assert no_sleep == [0.5, 1.0, 2.0, 4.0, 8.0]


def test_execute_backoff_respects_cap(no_sleep, deterministic_jitter, monkeypatch):
    monkeypatch.setattr(sc, "_RETRY_CAP_S", 2.0)
    throttled_err = TransportQueryError(
        "t", errors=[{"extensions": {"code": "THROTTLED"}, "message": "Throttled"}]
    )
    client = _make_scripted([throttled_err] * 6)
    with pytest.raises(TransientShopifyError):
        client.execute("{ __typename }")
    assert no_sleep == [0.5, 1.0, 2.0, 2.0, 2.0]


def test_execute_non_dict_raises_shopify_error_no_retry(no_sleep):
    client = _make_scripted(["unauthorized"])
    with pytest.raises(ShopifyError, match=r"non-dict response.*type=str"):
        client.execute("{ __typename }")
    assert no_sleep == []
    assert client._client.calls == 1


# ===========================================================================
# poll_job() backoff
# ===========================================================================


class _SleepTrackingClock:
    """Fake time module: monotonic() returns elapsed time; sleep(s) advances it."""

    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _make_always_not_done_client():
    """ShopifyClient stub whose execute() always returns done=False."""

    class _AlwaysNotDone:
        def execute(self, *_a, **_kw):
            return {"node": {"id": "gid://shopify/Job/1", "done": False}}

    client = object.__new__(ShopifyClient)
    client._client = _AlwaysNotDone()
    return client


def _make_done_after_n_client(n: int):
    """ShopifyClient stub that returns done=True on the n-th execute call (1-indexed)."""

    class _DoneAfterN:
        def __init__(self):
            self.calls = 0

        def execute(self, *_a, **_kw):
            self.calls += 1
            done = self.calls >= n
            return {"node": {"id": "gid://shopify/Job/1", "done": done}}

    client = object.__new__(ShopifyClient)
    client._client = _DoneAfterN()
    return client


def _patch_time(monkeypatch, clock):
    monkeypatch.setattr(sc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(sc.time, "sleep", clock.sleep)


def test_poll_job_default_uses_exponential_backoff(monkeypatch):
    from shopify_client import poll_job

    clock = _SleepTrackingClock()
    _patch_time(monkeypatch, clock)
    poll_job(_make_always_not_done_client(), "gid://shopify/Job/1", timeout_s=30)
    # First four sleeps: 0.5, 1, 2, 4 (exponential up to cap=5)
    assert clock.sleeps[:4] == [0.5, 1.0, 2.0, 4.0]
    # Subsequent sleeps hit the cap
    assert all(s == 5.0 for s in clock.sleeps[4:])


def test_poll_job_explicit_interval_overrides_backoff(monkeypatch):
    from shopify_client import poll_job

    clock = _SleepTrackingClock()
    _patch_time(monkeypatch, clock)
    poll_job(_make_always_not_done_client(), "gid://shopify/Job/1", timeout_s=10, interval_s=1.0)
    assert all(s == 1.0 for s in clock.sleeps)


def test_poll_job_first_response_done_no_sleep(monkeypatch):
    from shopify_client import poll_job

    clock = _SleepTrackingClock()
    _patch_time(monkeypatch, clock)
    result = poll_job(_make_done_after_n_client(1), "gid://shopify/Job/1", timeout_s=10)
    assert result["done"] is True
    assert result["timed_out"] is False
    assert clock.sleeps == []


def test_poll_job_done_after_one_step(monkeypatch):
    from shopify_client import poll_job

    clock = _SleepTrackingClock()
    _patch_time(monkeypatch, clock)
    result = poll_job(_make_done_after_n_client(2), "gid://shopify/Job/1", timeout_s=10)
    assert result["done"] is True
    assert clock.sleeps == [0.5]


def test_poll_job_budget_respects_next_sleep_size(monkeypatch):
    from shopify_client import poll_job

    clock = _SleepTrackingClock()
    _patch_time(monkeypatch, clock)
    # timeout_s=3: after 0.5+1.0=1.5s elapsed, next sleep=2.0 → 1.5+2.0=3.5 > 3 → exit
    result = poll_job(_make_always_not_done_client(), "gid://shopify/Job/1", timeout_s=3)
    assert result["timed_out"] is True
    assert clock.sleeps == [0.5, 1.0]
