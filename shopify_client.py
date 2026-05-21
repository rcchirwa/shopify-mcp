"""
Shopify Admin GraphQL API wrapper.
Loads credentials from .env — never hardcode secrets here.
"""

import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from gql import Client, gql
from gql.transport.exceptions import TransportQueryError, TransportServerError
from gql.transport.requests import RequestsHTTPTransport

# Some Shopify mutations (collectionAddProductsV2, collectionRemoveProducts, …)
# return a Job node rather than completing inline. `node(id)` lets us resolve
# any gid to its underlying type; the inline `... on Job` fragment exposes
# `done`, which flips to `true` once the server-side work has finished.
JOB_STATUS_QUERY = """
query JobStatus($id: ID!) {
  node(id: $id) {
    ... on Job { id done }
  }
}
"""

# Default budget for poll_job(). Single-item Shopify jobs usually come back
# done-on-first-response; this budget only matters for jobs that genuinely
# run async. Exposed as a constant so tool modules can cite the same value
# in user-visible timeout messages without re-declaring it.
JOB_POLL_TIMEOUT_S = 10

# --- Retry/backoff knobs (M5 security finding) ----------------------------
# Applied internally by ShopifyClient.execute() — callers only see success
# or a final RuntimeError after retries are exhausted. Full jitter prevents
# thundering-herd from parallel callers racing the same cost bucket.
_RETRY_MAX_ATTEMPTS = 5  # initial try + 5 retries = 6 total executions
_RETRY_BASE_S = 0.5
_RETRY_CAP_S = 30.0

# poll_job() backoff: tighter cap since polls are informational.
_POLL_BASE_S = 0.5
_POLL_CAP_S = 5.0

# HTTP status codes that should trigger a retry rather than a hard fail.
_RETRYABLE_HTTP_STATUSES = (429, 500, 502, 503, 504)
# Pre-compiled regex derived from the tuple above.
#
# Pattern: (?<![/\w])(<codes>)\b
#
# The leading negative lookbehind rejects digits that are immediately preceded
# by "/" (bare URL path segment, e.g. /resource/503/details) or by a word
# character (e.g. v503, api503).  \b at the end rejects trailing word chars
# (e.g. 503abc).  Together these ensure only a "standalone" status code in the
# error message — typically at the very start of gql's TransportServerError
# string, e.g. "503 Service Unavailable for url: …" — is treated as retryable.
_RETRYABLE_HTTP_RE = re.compile(
    r"(?<![/\w])(" + "|".join(str(c) for c in _RETRYABLE_HTTP_STATUSES) + r")\b"
)

# Pin .env to the repo root (next to this file) so loading is independent of
# the working directory the MCP process is launched with. Claude Desktop
# launches subprocesses with CWD=/, which makes the default `load_dotenv()`
# (which walks up from CWD) silently find nothing. `override=True` makes the
# on-disk file the source of truth — so a token rotated in .env wins over
# stale values injected by the launcher's config.
_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _mask_token(token: str) -> str:
    """Mask an access token for logging: preserve prefix + last 4 chars."""
    if not token:
        return "(empty)"
    if len(token) <= 8:
        return "*" * len(token)
    # Shopify admin tokens start with `shpat_`; preserve that hint if present.
    prefix = "shpat_" if token.startswith("shpat_") else token[:4]
    return f"{prefix}…{token[-4:]}"


class ShopifyError(RuntimeError):
    """Permanent Shopify failure — do not retry (4xx other than 429,
    schema/permission errors, malformed mutations)."""


class TransientShopifyError(RuntimeError):
    """Transient Shopify failure — safe to retry (THROTTLED, 429, 5xx).
    Surfaces to callers only after retries are exhausted."""


def _is_throttled(errors: Any) -> bool:
    """Return True iff a TransportQueryError.errors payload signals THROTTLED.

    Checks both `extensions.code == "THROTTLED"` (dict shape) and
    "THROTTLED" substring (string-shaped fallback).
    """
    if errors is None:
        return False
    if isinstance(errors, str):
        return "THROTTLED" in errors
    if not isinstance(errors, list):
        return "THROTTLED" in str(errors)
    for err in errors:
        if isinstance(err, dict):
            ext = err.get("extensions") or {}
            if isinstance(ext, dict) and ext.get("code") == "THROTTLED":
                return True
            msg = err.get("message") or ""
            if isinstance(msg, str) and "THROTTLED" in msg:
                return True
        elif "THROTTLED" in (err if isinstance(err, str) else str(err)):
            return True
    return False


def _is_retryable_http(exc: TransportServerError) -> bool:
    """Return True iff a TransportServerError is retryable (429 or 5xx).

    Uses a word-boundary regex on str(exc) to avoid false-positives from
    status-code digits appearing in URL paths or error bodies (e.g. /v500/).
    gql 4.0 has no structured status attribute, so string matching is
    unavoidable; \b ensures "v503" or "503abc" are not treated as status codes.
    """
    return bool(_RETRYABLE_HTTP_RE.search(str(exc)))


def _backoff_delay(attempt: int, *, base: float, cap: float, jitter: bool) -> float:
    """Compute the capped exponential backoff delay without sleeping.

    `attempt` is 0-indexed (first retry = attempt 0). Returns uniform [0,
    ceiling] when `jitter=True` (AWS full-jitter, recommended for contention
    recovery), or the ceiling itself when `jitter=False` (deterministic, used
    by poll_job where a single caller needs no herd-smearing).
    """
    ceiling = min(cap, base * (2**attempt))
    return random.uniform(0, ceiling) if jitter else ceiling


def _backoff_sleep(attempt: int, *, base: float, cap: float, jitter: bool) -> None:
    """Compute and perform one backoff sleep. See `_backoff_delay` for params."""
    time.sleep(_backoff_delay(attempt, base=base, cap=cap, jitter=jitter))


class ShopifyClient:
    def __init__(self) -> None:
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
        store_url = os.getenv("SHOPIFY_STORE_URL")
        access_token = os.getenv("SHOPIFY_ACCESS_TOKEN")
        # 2026-01: current stable, one release behind the edge. Every mutation
        # and query we use has a response shape unchanged since 2024-07 (where
        # `InventoryLevel.available` was removed in favor of
        # `quantities(names: ["available"])`), so older pins will fail the
        # inventory queries.
        api_version = os.getenv("SHOPIFY_API_VERSION", "2026-01")

        if not store_url or not access_token:
            raise ValueError("SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN must be set in .env")

        # Log the active credential fingerprint to stderr so operators can tell
        # at a glance which token is live without reading .env. Goes to stderr
        # (not stdout) to keep MCP's stdout JSON-RPC channel clean.
        env_src = ".env" if _ENV_PATH.is_file() else "process env"
        print(
            f"[shopify_client] store={store_url} "
            f"api_version={api_version} "
            f"token={_mask_token(access_token)} "
            f"source={env_src}",
            file=sys.stderr,
        )

        transport = RequestsHTTPTransport(
            url=f"https://{store_url}/admin/api/{api_version}/graphql.json",
            headers={"X-Shopify-Access-Token": access_token},
            timeout=15,
        )
        self._client = Client(
            transport=transport,
            fetch_schema_from_transport=False,
        )

    def execute(self, query_str: str, variables: dict | None = None) -> dict:
        gql_query = gql(query_str)
        for attempt in range(_RETRY_MAX_ATTEMPTS + 1):
            try:
                result = self._client.execute(gql_query, variable_values=variables or {})
            except TransportQueryError as e:
                if _is_throttled(e.errors):
                    if attempt < _RETRY_MAX_ATTEMPTS:
                        _backoff_sleep(attempt, base=_RETRY_BASE_S, cap=_RETRY_CAP_S, jitter=True)
                        continue
                    raise TransientShopifyError(
                        f"Shopify GraphQL THROTTLED after {attempt + 1} attempts: "
                        f"{_format_errors(e.errors)}"
                    ) from e
                raise ShopifyError(f"Shopify GraphQL error: {_format_errors(e.errors)}") from e
            except TransportServerError as e:
                if _is_retryable_http(e):
                    if attempt < _RETRY_MAX_ATTEMPTS:
                        _backoff_sleep(attempt, base=_RETRY_BASE_S, cap=_RETRY_CAP_S, jitter=True)
                        continue
                    raise TransientShopifyError(
                        f"Shopify HTTP error after {attempt + 1} attempts: {e!s}"
                    ) from e
                raise ShopifyError(f"Shopify HTTP error: {e!s}") from e

            if not isinstance(result, dict):
                # Surface the real payload (scope error text, HTML error page, etc.)
                # so callers don't crash downstream with 'str' object has no attribute 'get'.
                preview = str(result)[:500]
                raise ShopifyError(
                    f"Shopify returned non-dict response (type={type(result).__name__}): {preview}"
                )
            return result

        raise TransientShopifyError("Shopify retry loop exhausted")  # pragma: no cover


def poll_job(
    client: "ShopifyClient",
    job_gid: str,
    timeout_s: int = JOB_POLL_TIMEOUT_S,
    interval_s: float | None = None,
) -> dict:
    """
    Poll a Shopify Job node until `done=true` or the budget is exhausted.

    Returns a dict with keys:
      - id: str            — the job gid (echoed for logging)
      - done: bool         — final observed `done` value (False on timeout)
      - elapsed_s: float   — wall-clock time spent polling
      - timed_out: bool    — True iff the budget was exhausted before done
      - error: str or None — transport error from the last failed poll

    When `interval_s` is None (default), uses capped exponential backoff
    (0.5s, 1s, 2s, 4s, 5s, 5s …). Pass an explicit float to override with
    a fixed sleep interval.

    Does NOT raise. The underlying mutation has already succeeded by the time
    the caller invokes this — polling is strictly informational.
    """
    start = time.monotonic()
    last_error: str | None = None
    last_done: bool = False
    attempt = 0
    while True:
        try:
            result = client.execute(JOB_STATUS_QUERY, {"id": job_gid})
            node = (result or {}).get("node") or {}
            last_done = bool(node.get("done"))
            last_error = None
        except Exception as e:
            # Reset done on failure so a stale True from a prior iteration
            # can't combine with a later failed poll to misreport success.
            last_done = False
            last_error = str(e)

        elapsed = time.monotonic() - start
        if last_done:
            return {
                "id": job_gid,
                "done": True,
                "elapsed_s": elapsed,
                "timed_out": False,
                "error": None,
            }

        next_sleep = (
            interval_s
            if interval_s is not None
            else _backoff_delay(attempt, base=_POLL_BASE_S, cap=_POLL_CAP_S, jitter=False)
        )

        if elapsed + next_sleep > timeout_s:
            return {
                "id": job_gid,
                "done": False,
                "elapsed_s": elapsed,
                "timed_out": True,
                "error": last_error,
            }
        time.sleep(next_sleep)
        attempt += 1


def _format_errors(errors: Any) -> str:
    # `TransportQueryError.errors` is typed Optional[List[Any]] in gql 4.0 — in
    # practice it can be a list of dicts, a list of GraphQLError objects, a
    # list of strings, a single string, or None. Earlier versions of this
    # handler assumed list-of-dicts and crashed with
    # `'str' object has no attribute 'get'` on the other shapes, masking the
    # real Shopify error from callers.
    if errors is None:
        return "(no error details)"
    if isinstance(errors, str):
        return errors
    if not isinstance(errors, list):
        return str(errors)
    return "; ".join(_format_one_error(err) for err in errors)


def _format_one_error(err: Any) -> str:
    if isinstance(err, dict):
        return err.get("message") or str(err)
    if isinstance(err, str):
        return err
    return str(err)
