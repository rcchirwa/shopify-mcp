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
from gql.transport.exceptions import (
    TransportProtocolError,
    TransportQueryError,
    TransportServerError,
)
from gql.transport.requests import RequestsHTTPTransport

from shopify_mcp.logging_config import configure_logging
from shopify_mcp.settings import Settings
from shopify_mcp.shopify._cache import ShopifyMetadataCache

# Intentional client→tools import (A6): ShopifyClient is now the single HTTP
# chokepoint, so it owns the shared header policy and the SSRF guard. Both are
# leaf modules (tools/_http, tools/_url_safety import nothing from here), so
# this does not create an import cycle.
from shopify_mcp.tools._http import default_headers

# Aliased: `_backoff_delay` takes a float parameter named `cap`, so importing
# the bound-text helper under its bare name would shadow it there.
from shopify_mcp.tools._scrub import cap as cap_text
from shopify_mcp.tools._scrub import sanitize_control_chars

# Also a leaf (imports only the standard library and tools/_scrub): fences the
# third-party text fetch_bytes reflects.
from shopify_mcp.tools._untrusted import wrap_reflected
from shopify_mcp.tools._url_safety import _reject_if_private_host

# Return type of the callable handed to ShopifyClient._with_retry.
_T = TypeVar("_T")

# Some Shopify mutations (collectionAddProductsV2, collectionRemoveProducts, …)
# return a Job rather than completing inline; `done` flips to `true` once the
# server-side work has finished.
#
# Story 9.18: this used to be `node(id: $id) { ... on Job { id done } }`, which
# cannot work on 2026-01 — `Job` implements no interfaces, so it is not a `Node`
# and the inline fragment can never match ("Fragment cannot be spread here as
# objects of type 'Node' can never be of type 'Job'"). `QueryRoot.job(id:)` is
# the replacement and returns `Job` directly.
#
# The response key below is load-bearing: `poll_job` reads the top-level field
# this document selects, so the two must change together. A test in
# tests/unit/test_client.py derives the key from this parsed query rather than
# restating it, because fixing one without the other leaves the whole suite
# green while live polling never completes.
JOB_STATUS_QUERY = """
query JobStatus($id: ID!) {
  job(id: $id) { id done }
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

# Response header naming the Admin API version Shopify actually served.
#
# Shopify does not reject an unsupported SHOPIFY_API_VERSION pin — it answers
# HTTP 200 on the oldest still-supported version and names that version here.
# Story 9.12's outage is what that silence costs: the pin sat at a retired
# 2024-01 while the API moved underneath it, and nothing surfaced until fields
# the queries depended on were removed. Story 9.13 reads the header so the
# drift is visible the moment it starts rather than whenever it first breaks.
_API_VERSION_HEADER = "X-Shopify-Api-Version"

# Pin .env to the repo root so loading is independent of the working directory
# the MCP process is launched with. Claude Desktop launches subprocesses with
# CWD=/, which makes the default `load_dotenv()` (which walks up from CWD)
# silently find nothing. `override=True` makes the on-disk file the source of
# truth — so a token rotated in .env wins over stale values injected by the
# launcher's config.
#
# parents[2], not parent: the src/ move (Story 10.47 / FS-3) put this module at
# <repo root>/src/shopify_mcp/client.py, two directories below the root.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

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
        elif "THROTTLED" in (err if isinstance(err, str) else str(err)):  # reflect-ok: predicate
            return True
    return False


def _is_retryable_http(exc: TransportServerError) -> bool:
    """Return True iff a TransportServerError is retryable (429 or 5xx).

    Uses a word-boundary regex on str(exc) to avoid false-positives from
    status-code digits appearing in URL paths or error bodies (e.g. /v500/).
    gql 4.0 has no structured status attribute, so string matching is
    unavoidable; \b ensures "v503" or "503abc" are not treated as status codes.
    """
    return bool(_RETRYABLE_HTTP_RE.search(str(exc)))  # reflect-ok: predicate


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

        # Held as an attribute (not just handed to Client) because gql records
        # each response's headers on the transport instance, and that is where
        # _warn_on_api_version_drift reads the served version from.
        self._transport = RequestsHTTPTransport(
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
        # The served version last warned about, so a persistent drift reports
        # once rather than on every query — but a *different* substitution
        # later in this process still gets its own line.
        self._warned_served_version: str | None = None
        self._client = Client(
            transport=self._transport,
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
                raise TransientShopifyError(
                    f"{cap_text(str(e))} after {attempt + 1} attempts"
                ) from e
        raise TransientShopifyError(f"{label} retry loop exhausted")  # pragma: no cover

    def _warn_on_api_version_drift(self) -> None:
        """Log a WARNING when Shopify serves a version we did not request.

        Runs after every call, successful or not — the call most likely to
        expose a drifted pin is the one that *fails* because the substituted
        version removed a field it selects.

        Warns once per distinct served version rather than once per call. The
        requested pin cannot change while the process runs, so repeating it
        every query would bury the signal; but Shopify's substitute *can*
        change under a long-lived process, because what it substitutes is the
        oldest still-supported version and that rotates. A second, different
        substitution is news, so it gets its own line.

        Stays silent whenever it cannot tell: headers are None until the first
        response lands, a response need not carry the header, and after a
        connection-level failure they still describe the previous call (same
        pinned URL, so the verdict stays true even then). Unexpected shapes are
        swallowed as well: this is observability watching the data path, and it
        must never be the reason that path fails — nor, running in `finally`,
        replace the real exception it is unwinding from.
        """
        try:
            headers = getattr(self._transport, "response_headers", None)
            if not headers:
                return
            served = headers.get(_API_VERSION_HEADER)
            requested = self._settings.shopify_api_version
            # isinstance, not `is None`: a non-str value would otherwise reach
            # sanitize_control_chars and raise out of an observability helper.
            if not isinstance(served, str) or served == requested:
                return
            if served == self._warned_served_version:
                return
            self._warned_served_version = served
            logger.warning(
                "Shopify served Admin API version %s but SHOPIFY_API_VERSION requests %s — "
                "the pinned version is unsupported and Shopify silently substituted the "
                "oldest supported one. Update SHOPIFY_API_VERSION in .env to a supported "
                "version; queries will otherwise break without warning as fields are removed.",
                # `served` is upstream-controlled text on its way into a log
                # line, so it gets the same treatment as every other reflected
                # value here (SEC-20, SEC-24): strip control characters that
                # could forge extra log lines, then bound the length.
                # `requested` is validated by Settings._validate_api_version.
                cap_text(sanitize_control_chars(served)),
                requested,
            )
        except Exception:
            # Deliberately broad. The only thing worse than missing a drift
            # warning is turning a working call — or a clean ShopifyError —
            # into an AttributeError from the watcher. Reachable today if gql
            # ever hands back a non-mapping `response_headers`.
            logger.debug("Admin API version drift check skipped", exc_info=True)

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
                    raise TransientShopifyError(f"Shopify HTTP error: {cap_text(str(e))}") from e
                raise ShopifyError(f"Shopify HTTP error: {cap_text(str(e))}") from e
            except TransportProtocolError as e:
                # gql raises this when Shopify returns something that is not a
                # GraphQL result — an HTML error page, a WAF interstitial, a
                # 200 with garbage — and it embeds the **entire raw response
                # body** in the message. It bypassed `_format_errors` entirely,
                # so it was the single largest remaining path for unbounded
                # upstream text to reach model context. Bounded here, at the
                # only place that sees it. (Story 10.67 / SEC-27, round 2.)
                raise ShopifyError(f"Shopify protocol error: {cap_text(str(e))}") from e

            if not isinstance(result, dict):
                # Surface the real payload (scope error text, HTML error page, etc.)
                # so callers don't crash downstream with 'str' object has no attribute 'get'.
                # Sanitize before capping (Story 10.58 / SEC-24, L7) — matching
                # tools/_log.py::log_write's order — so escaped "\n"/"\r" tokens
                # count toward the REFLECT_MAX_LEN bound rather than the raw
                # control characters. SEC-27 (Story 10.67) already migrated this
                # from a hand-rolled `[:500]` slice to the shared `cap` bound at
                # REFLECT_MAX_LEN (300); this closes the remaining half — the
                # preview was still an unscrubbed slice with no control-character
                # stripping, so a CR/LF-bearing upstream payload could forge
                # extra lines wherever this exception message is logged.
                preview = cap_text(sanitize_control_chars(str(result)))
                raise ShopifyError(
                    f"Shopify returned non-dict response (type={type(result).__name__}): {preview}"
                )
            return result

        # op_name is extracted via regex from an in-code GraphQL query string
        # (never caller-controlled), so unlike fetch_bytes' `url` it carries
        # no injection risk and doesn't need sanitize_control_chars (SEC-20).
        # `finally`, not just the success path: the call most likely to expose a
        # drifted pin is the one that FAILS because the substituted version
        # removed a field the query selects — Story 9.12's outage exactly. gql
        # records the response headers before raising, so the served version is
        # just as readable there, and suppressing the warning would hide the
        # drift at the one moment it is doing visible damage (Story 9.13).
        try:
            return self._with_retry(_attempt, label=f"op={op_name}")
        finally:
            self._warn_on_api_version_drift()

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
            except (requests.RequestException, ValueError) as e:
                # A transport error (DNS, connection reset, read timeout) is
                # treated as permanent here — mirrors execute(), which only
                # retries on parsed transient statuses, not raw socket errors.
                # The text is fenced: it can quote the remote server's own bytes
                # (http.client's BadStatusLine repeats the status line).
                # ValueError too: requests resolves the redirect target even
                # with allow_redirects=False, and with NO_PROXY set a bad
                # Location port raises a bare ValueError quoting it (Story 10.95).
                raise ShopifyError(wrap_reflected("request failed: ", e)) from e

            status = resp.status_code
            # `allow_redirects=False` makes a 3xx a terminal response. Refuse it:
            # following a redirect would re-issue the request to the Location
            # host without re-running the SSRF guard, re-opening the bypass.
            if 300 <= status < 400:
                # The Location value is written by whatever server the caller's
                # URL points at, so it is fenced as untrusted (Story 10.95). An
                # empty header reads as an absent one, never as a bare fence.
                head = f"HTTP {status} redirect to "
                tail = (
                    " — refused; redirects can bypass the SSRF guard. "
                    "Supply the final URL directly."
                )
                location = resp.headers.get("Location")
                if not location:
                    raise ShopifyError(f"{head}(no Location header){tail}")
                raise ShopifyError(wrap_reflected(head, location, tail))
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
            try:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > max_size:
                        raise ShopifyError(
                            f"source exceeded the {_human_bytes(max_size)} cap during download"
                        )
            except requests.RequestException as e:
                # A mid-body transport failure is raised here, outside the try
                # around requests.get(). Permanent like that one, and fenced for
                # the same reason: urllib3's InvalidChunkLength quotes the
                # server's chunk-size line (Story 10.95).
                raise ShopifyError(wrap_reflected("download failed: ", e)) from e

            content_type = resp.headers.get("Content-Type") or ""
            return bytes(buf), content_type

        # `url` is caller-supplied and reachable via indirect prompt injection
        # (SEC-20); the retry warning logs `label` through a single-line
        # stderr formatter that never neutralizes CR/LF, so an embedded
        # newline could forge a second, spoofed-looking log entry. Escape it
        # the same way the audit log already does (tools/_log.py).
        return self._with_retry(_attempt, label=f"fetch {sanitize_control_chars(url)}")

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

        Returns (first_page_response, all_nodes, capped). Callers can extract
        non-paginated fields (e.g. product.title) from first_page_response.

        capped=True means the walk stopped SHORT of the end of the connection,
        for any of three reasons (Story 10.78 — it used to name only the first):
          - max_pages was exhausted before hasNextPage turned False;
          - Shopify returned hasNextPage=True with endCursor=null, leaving
            nowhere to continue from;
          - the connection vanished mid-walk — the parent went null, or the
            key went missing, on page 1 or later.
        In every case the nodes collected so far are still returned. capped is
        never True for a walk that reached the end of the connection.
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
            # Story 10.78: on page 0 the `or {}` collapse above means "nothing
            # to read" and is load-bearing — Story 10.76's not-found path maps a
            # page-0 `collectionByHandle: null` to None from exactly this shape.
            # On page 1+ the same collapse means something else entirely: the
            # thing being walked went away between requests, and the previous
            # page's hasNextPage=True proves more of it existed. Without this
            # branch the absent pageInfo reads as hasNextPage=False and the
            # partial result is handed back as complete.
            #
            # The test is `not connection`, i.e. "resolved to nothing at all" —
            # NOT "has no nodes". A connection legitimately returning
            # {"nodes": [], "pageInfo": {...}} is a populated dict, so it stays
            # truthy and this branch leaves it alone.
            if page > 0 and not connection:
                logger.warning(
                    "paginate: connection=%s vanished on page=%d "
                    "(previous page reported hasNextPage=True); aborting",
                    connection_path,
                    page,
                )
                break
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
        # Reached by all three stop-short paths, so it does not name a cause —
        # the two abnormal ones log their own line above with the specifics.
        # It used to read "paginate capped … max_pages=%d", which described the
        # budget path and misdescribed the other two (Story 10.78). max_pages is
        # still reported because it is the budget the walk ran under either way.
        logger.warning(
            "paginate stopped short connection=%s max_pages=%d nodes=%d",
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
    Poll a Shopify Job until `done=true` or the budget is exhausted.

    Returns a dict with keys:
      - id: str            — the job gid (echoed for logging)
      - done: bool         — final observed `done` value (False on timeout)
      - elapsed_s: float   — wall-clock time spent polling
      - timed_out: bool    — True iff the budget was exhausted before done
      - error: str or None — error from the last failed poll. Can be set with
        timed_out=False: a permanent ShopifyError (Story 10.89) fails fast on
        the first poll rather than retrying to budget, since retrying a
        validation/auth/schema error cannot help and reporting it as a
        timeout hides the real cause.

    When `interval_s` is None (default), uses capped exponential backoff
    (0.5s, 1s, 2s, 4s, 5s, 5s …). Pass an explicit float to override with
    a fixed sleep interval.

    Does NOT raise. The underlying mutation has already succeeded by the time
    the caller invokes this — polling is strictly informational.

    Story 9.18 — unknown/expired job ids now read as DONE. `QueryRoot.job(id:)`
    answers `{"done": true}` for an id it does not recognise, where the removed
    `node(id:)` shape returned null and read as not-done until the budget ran
    out. So `timed_out` is now reachable essentially only through transport
    errors. This is deliberately NOT compensated for: by the time `poll_job`
    runs the mutation has already succeeded, so reporting an unrecognised job
    as finished is the same answer the caller would have got after waiting, and
    reaching it immediately is strictly better than burning the full timeout.
    The timeout branch stays meaningful for the case that still matters — a
    real job whose polls keep failing in transport.
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
            job = (result or {}).get("job") or {}
            last_done = bool(job.get("done"))
            last_error = None
        except ShopifyError as e:
            # Story 10.89: a permanent failure (validation, auth, schema
            # drift) — retrying to budget cannot help, and the broad except
            # below used to do exactly that, reporting timed_out=True and
            # hiding the real cause behind a "still running" story. Fail
            # fast instead. ShopifyError and TransientShopifyError are
            # siblings under RuntimeError (see their class docstrings), so
            # this branch cannot swallow a transient failure — that still
            # falls through to the broad except and loops to budget.
            return {
                "id": job_gid,
                "done": False,
                "elapsed_s": time.monotonic() - start,
                "timed_out": False,
                "error": cap_text(str(e)),
            }
        except Exception as e:
            # Reset done on failure so a stale True from a prior iteration
            # can't combine with a later failed poll to misreport success.
            last_done = False
            # Capped at the source (Story 10.67 / SEC-27): this string is
            # returned as the job result's "error" and reflected verbatim by
            # `tools/collections.py`, which is how an unbounded body survived
            # the first pass of this story's sweep.
            last_error = cap_text(str(e))

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


def _bound(text: str) -> str:
    """Bound upstream text and say so when something was dropped.

    Story 10.67 (SEC-27). `cap_text` truncates silently, which is fine for a
    single identifier but not here: `_format_errors` joins multiple Shopify
    errors with "; ", so a silent cut can delete the 2nd and 3rd messages
    entirely — and an access-denied or missing-scope error is not always first.
    The marker means a reader can tell "Shopify said one thing" from "Shopify
    said three things and you are seeing one". Still `_scrub.cap` underneath;
    this adds a suffix, it does not introduce a second slicing implementation.
    """
    bounded = cap_text(text)
    return bounded if bounded == text else f"{bounded} …[truncated]"


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
        return _bound(errors)
    if not isinstance(errors, list):
        return _bound(str(errors))
    # Story 10.67 (SEC-27) bounds the join. This is the constructor for the
    # message every ShopifyError carries, and it concatenates arbitrary upstream
    # text — so it is the actual unbounded source, not the call sites that echo
    # it. Capping here means a consumer that forgets to cap (as
    # `tools/collections.py` did, via poll_job) cannot leak an unbounded body.
    # The call sites still cap; this is the backstop that makes enumerating
    # them unnecessary for safety.
    return _bound("; ".join(_format_one_error(err) for err in errors))


def _format_one_error(err: Any) -> str:
    if isinstance(err, dict):
        return err.get("message") or str(err)  # reflect-ok: capped by _format_errors
    if isinstance(err, str):
        return err
    return str(err)  # reflect-ok: capped by _format_errors
