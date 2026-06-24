"""
Shopify Admin GraphQL API wrapper.
Loads credentials from .env — never hardcode secrets here.
"""

import logging
import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import requests
from dotenv import load_dotenv
from gql import Client, gql
from gql.transport.exceptions import TransportQueryError, TransportServerError
from gql.transport.requests import RequestsHTTPTransport

from logging_config import configure_logging
from settings import Settings
from shopify._cache import ShopifyMetadataCache

# Intentional client→tools import (A6): ShopifyClient is now the single HTTP
# chokepoint, so it owns the shared header policy and the SSRF guard. Both are
# leaf modules (tools/_http, tools/_url_safety import nothing from here), so
# this does not create an import cycle.
from tools._http import default_headers
from tools._url_safety import _reject_if_private_host

# Return type of the callable handed to ShopifyClient._with_retry.
_T = TypeVar("_T")

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

# Retry/backoff and poll-timeout knobs now live on Settings (item A7). The
# constants below were promoted to Settings fields so tests can override via
# a Settings instance and ops can tune them via env vars without code edits.

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

# Extracts the operation name from a GQL query string. Matches the first named
# query/mutation/subscription; falls back to "<anonymous>" for shorthand queries.
_GQL_OP_NAME_RE = re.compile(r"(?:query|mutation|subscription)\s+(\w+)", re.IGNORECASE)

# Pin .env to the repo root (next to this file) so loading is independent of
# the working directory the MCP process is launched with. Claude Desktop
# launches subprocesses with CWD=/, which makes the default `load_dotenv()`
# (which walks up from CWD) silently find nothing. `override=True` makes the
# on-disk file the source of truth — so a token rotated in .env wins over
# stale values injected by the launcher's config.
_ENV_PATH = Path(__file__).resolve().parent / ".env"

logger = logging.getLogger(__name__)


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


def _human_bytes(n: int) -> str:
    """Format a byte count for operator-facing error text (e.g. "20.00 MB").

    Kept here (rather than reusing the media-layer formatter) so fetch_bytes —
    the generic HTTP chokepoint — has no dependency back into tools.media.
    """
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _backoff_delay(attempt: int, *, base: float, cap: float, jitter: bool) -> float:
    """Compute the capped exponential backoff delay without sleeping.

    `attempt` is 0-indexed (first retry = attempt 0). Returns uniform [0,
    ceiling] when `jitter=True` (AWS full-jitter, recommended for contention
    recovery), or the ceiling itself when `jitter=False` (deterministic, used
    by poll_job where a single caller needs no herd-smearing).
    """
    ceiling = min(cap, base * (2**attempt))
    return random.uniform(0, ceiling) if jitter else ceiling


class ShopifyClient:
    def __init__(self, settings: Settings | None = None) -> None:
        # load_dotenv stays first so process env reflects on-disk .env before
        # Settings() reads it. _ENV_PATH override=True preserves the existing
        # semantic that .env wins over stale env vars injected by the launcher.
        load_dotenv(dotenv_path=_ENV_PATH, override=True)
        # pydantic-settings populates required fields from env vars — mypy
        # can't see that, so the bare Settings() call needs a type-ignore.
        self._settings = settings or Settings()  # type: ignore[call-arg]
        configure_logging(self._settings)

        # Cross-call TTL cache for stable Shopify metadata (channels today —
        # A8 / Story 10.32), constructed from the Settings-driven per-resource TTLs.
        self._metadata_cache = ShopifyMetadataCache(self._settings)

        # Log the active credential fingerprint to stderr so operators can tell
        # at a glance which token is live without reading .env. Goes to stderr
        # (not stdout) to keep MCP's stdout JSON-RPC channel clean.
        env_src = ".env" if _ENV_PATH.is_file() else "process env"
        access_token = self._settings.shopify_access_token.get_secret_value()
        logger.info(
            "store=%s api_version=%s token=%s source=%s",
            self._settings.shopify_store_url,
            self._settings.shopify_api_version,
            _mask_token(access_token),
            env_src,
        )

        transport = RequestsHTTPTransport(
            url=(
                f"https://{self._settings.shopify_store_url}"
                f"/admin/api/{self._settings.shopify_api_version}/graphql.json"
            ),
            headers={
                "X-Shopify-Access-Token": access_token,
                "User-Agent": self._settings.http_user_agent,
            },
            timeout=self._settings.request_timeout_s,
        )
        self._client = Client(
            transport=transport,
            fetch_schema_from_transport=False,
        )

    def _with_retry(self, attempt_fn: Callable[[], _T], *, label: str) -> _T:
        """Run ``attempt_fn`` with capped exponential backoff + jitter.

        The single backoff implementation shared by :meth:`execute` and
        :meth:`fetch_bytes` (A6 — exactly one retry loop). ``attempt_fn`` runs
        one attempt and either returns a result, raises :class:`ShopifyError`
        for a permanent failure (propagated immediately — no retry), or raises
        :class:`TransientShopifyError` for a retryable one (retried up to
        ``retry_max_attempts``, then re-raised once with an "after N attempts"
        suffix so callers can see the budget was exhausted).
        """
        max_attempts = self._settings.retry_max_attempts
        base_s = self._settings.retry_base_s
        cap_s = self._settings.retry_cap_s
        for attempt in range(max_attempts + 1):
            try:
                return attempt_fn()
            except TransientShopifyError as e:
                if attempt < max_attempts:
                    delay = _backoff_delay(attempt, base=base_s, cap=cap_s, jitter=True)
                    logger.warning("retryable %s attempt=%d sleep=%.2fs", label, attempt, delay)
                    time.sleep(delay)
                    continue
                raise TransientShopifyError(f"{e} after {attempt + 1} attempts") from e
        raise TransientShopifyError(f"{label} retry loop exhausted")  # pragma: no cover

    def execute(self, query_str: str, variables: dict | None = None) -> dict:
        gql_query = gql(query_str)
        _m = _GQL_OP_NAME_RE.search(query_str)
        op_name = _m.group(1) if _m else "<anonymous>"
        logger.debug("gql op=%s variables=%s", op_name, list((variables or {}).keys()))

        def _attempt() -> dict:
            try:
                result = self._client.execute(gql_query, variable_values=variables or {})
            except TransportQueryError as e:
                if _is_throttled(e.errors):
                    raise TransientShopifyError(
                        f"Shopify GraphQL THROTTLED: {_format_errors(e.errors)}"
                    ) from e
                raise ShopifyError(f"Shopify GraphQL error: {_format_errors(e.errors)}") from e
            except TransportServerError as e:
                if _is_retryable_http(e):
                    raise TransientShopifyError(f"Shopify HTTP error: {e!s}") from e
                raise ShopifyError(f"Shopify HTTP error: {e!s}") from e

            if not isinstance(result, dict):
                # Surface the real payload (scope error text, HTML error page, etc.)
                # so callers don't crash downstream with 'str' object has no attribute 'get'.
                preview = str(result)[:500]
                raise ShopifyError(
                    f"Shopify returned non-dict response (type={type(result).__name__}): {preview}"
                )
            return result

        return self._with_retry(_attempt, label=f"op={op_name}")

    def fetch_bytes(
        self,
        url: str,
        *,
        max_size: int,
        allow_redirects: bool = False,
    ) -> tuple[bytes, str]:
        """Fetch raw bytes from a caller-supplied URL through the shared policy.

        The single chokepoint for non-GraphQL GETs (A6): runs the SSRF guard,
        sends the shared User-Agent (``tools._http.default_headers``), uses the
        configured download timeout, streams with a hard ``max_size`` cap, and
        retries retryable statuses (429/5xx) using the same backoff as
        :meth:`execute` via :meth:`_with_retry`. Returns ``(body, content_type)``
        on a 2xx response; ``content_type`` is the raw ``Content-Type`` header
        (possibly empty) for the caller to validate.

        Raises :class:`ShopifyError` on a permanent failure (a refused redirect
        with ``allow_redirects=False``, a non-retryable ``>= 400`` status, a
        transport error, or the size cap being exceeded) and
        :class:`TransientShopifyError` when a retryable status outlives the
        retry budget. The SSRF guard rejects a private/loopback host by raising
        a bare :class:`RuntimeError` (from ``_reject_if_private_host``) before
        any request — callers that catch only the Shopify error types must also
        expect that.
        """
        # SSRF guard runs once, on the original URL, before any request — its
        # verdict can't change between retries, so it sits outside the loop.
        _reject_if_private_host(url)
        headers = default_headers(self._settings)
        timeout = self._settings.download_timeout_s

        def _attempt() -> tuple[bytes, str]:
            try:
                resp = requests.get(
                    url,
                    stream=True,
                    timeout=timeout,
                    allow_redirects=allow_redirects,
                    headers=headers,
                )
            except requests.RequestException as e:
                # A transport error (DNS, connection reset, read timeout) is
                # treated as permanent here — mirrors execute(), which only
                # retries on parsed transient statuses, not raw socket errors.
                raise ShopifyError(f"request failed: {e}") from e

            status = resp.status_code
            # `allow_redirects=False` makes a 3xx a terminal response. Refuse it:
            # following a redirect would re-issue the request to the Location
            # host without re-running the SSRF guard, re-opening the bypass.
            if 300 <= status < 400:
                location = resp.headers.get("Location", "(no Location header)")
                raise ShopifyError(
                    f"HTTP {status} redirect to {location} — refused; redirects can "
                    f"bypass the SSRF guard. Supply the final URL directly."
                )
            if status in _RETRYABLE_HTTP_STATUSES:
                raise TransientShopifyError(f"HTTP {status} from source URL")
            if status >= 400:
                raise ShopifyError(f"HTTP {status} from source URL")

            # Content-Length is advisory — refuse an over-cap file before pulling
            # bytes; the streaming loop below enforces the cap again regardless.
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > max_size:
                raise ShopifyError(
                    f"source is {_human_bytes(int(cl))} — exceeds the {_human_bytes(max_size)} cap"
                )

            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_size:
                    raise ShopifyError(
                        f"source exceeded the {_human_bytes(max_size)} cap during download"
                    )

            content_type = resp.headers.get("Content-Type") or ""
            return bytes(buf), content_type

        return self._with_retry(_attempt, label=f"fetch {url}")

    def paginate(
        self,
        query_str: str,
        variables: dict[str, Any],
        *,
        connection_path: list[str],
        page_size: int = 50,
        max_pages: int = 10,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        """Walk pageInfo cursor pagination across multiple Shopify requests.

        The query must accept $first: Int! and $after: String variables and
        select pageInfo { hasNextPage endCursor } on the paginated connection.

        Returns (first_page_response, all_nodes, capped) where capped=True
        when max_pages was exhausted before hasNextPage turned False. Callers
        can extract non-paginated fields (e.g. product.title) from first_page_response.
        """
        all_nodes: list[dict[str, Any]] = []
        first_response: dict[str, Any] = {}
        cursor: str | None = None
        for page in range(max_pages):
            page_vars: dict[str, Any] = {**variables, "first": page_size, "after": cursor}
            result = self.execute(query_str, page_vars)
            if page == 0:
                first_response = result
            connection: Any = result
            for key in connection_path:
                connection = (connection or {}).get(key) or {}
            all_nodes.extend(list(connection.get("nodes") or []))
            page_info: dict[str, Any] = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return first_response, all_nodes, False
            cursor = page_info.get("endCursor")
            if cursor is None:
                logger.warning(
                    "paginate: hasNextPage=True but endCursor=null on connection=%s; aborting",
                    connection_path,
                )
                break
        logger.warning(
            "paginate capped connection=%s max_pages=%d nodes=%d",
            connection_path,
            max_pages,
            len(all_nodes),
        )
        return first_response, all_nodes, True


def poll_job(
    client: "ShopifyClient",
    job_gid: str,
    timeout_s: float | None = None,
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
    effective_timeout = timeout_s if timeout_s is not None else client._settings.job_poll_timeout_s
    poll_base = client._settings.poll_base_s
    poll_cap = client._settings.poll_cap_s
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
            else _backoff_delay(attempt, base=poll_base, cap=poll_cap, jitter=False)
        )

        if elapsed + next_sleep > effective_timeout:
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
