"""
Shopify Admin GraphQL API wrapper.
Loads credentials from .env — never hardcode secrets here.
"""

import os
import sys
import time
from pathlib import Path

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

# Pin .env to the repo root (next to this file) so loading is independent of
# the working directory the MCP process is launched with. Claude Desktop
# launches subprocesses with CWD=/, which makes the default `load_dotenv()`
# (which walks up from CWD) silently find nothing. `override=True` makes the
# on-disk file the source of truth — so a token rotated in .env wins over
# stale values injected by the launcher's config.
_ENV_PATH = Path(__file__).resolve().parent / ".env"


def to_gid(resource_type: str, numeric_id) -> str:
    return f"gid://shopify/{resource_type}/{numeric_id}"


def from_gid(gid: str) -> str:
    # Tolerate None/empty so callers can pass `obj.get("id")` or
    # `obj.get("id", "")` without a pre-check — Shopify responses may
    # return `id: null` on partial/permissions-trimmed fields, and the
    # dict .get(..., "") default doesn't catch the "key present, value None" case.
    if not gid:
        return ""
    return gid.split("/")[-1]


def with_confirm_hint(preview: str) -> str:
    """Append the confirm hint used by every write-tool preview branch."""
    return preview + "\n\nTo apply, call again with confirm=True."


def _mask_token(token: str) -> str:
    """Mask an access token for logging: preserve prefix + last 4 chars."""
    if not token:
        return "(empty)"
    if len(token) <= 8:
        return "*" * len(token)
    # Shopify admin tokens start with `shpat_`; preserve that hint if present.
    prefix = "shpat_" if token.startswith("shpat_") else token[:4]
    return f"{prefix}…{token[-4:]}"


class ShopifyClient:
    def __init__(self):
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
        try:
            result = self._client.execute(gql(query_str), variable_values=variables or {})
        except TransportQueryError as e:
            raise RuntimeError(f"Shopify GraphQL error: {_format_errors(e.errors)}") from e
        except TransportServerError as e:
            raise RuntimeError(f"Shopify HTTP error: {e!s}") from e

        if not isinstance(result, dict):
            # Surface the real payload (scope error text, HTML error page, etc.)
            # so callers don't crash downstream with 'str' object has no attribute 'get'.
            preview = str(result)[:500]
            raise RuntimeError(
                f"Shopify returned non-dict response (type={type(result).__name__}): {preview}"
            )
        return result


def poll_job(
    client: "ShopifyClient",
    job_gid: str,
    timeout_s: int = 10,
    interval_s: float = 1.0,
) -> dict:
    """
    Poll a Shopify Job node until `done=true` or the budget is exhausted.

    Returns a dict with keys:
      - id: str            — the job gid (echoed for logging)
      - done: bool         — final observed `done` value (False on timeout)
      - elapsed_s: float   — wall-clock time spent polling
      - timed_out: bool    — True iff the budget was exhausted before done
      - error: str or None — transport error from the last failed poll

    Does NOT raise. The underlying mutation has already succeeded by the time
    the caller invokes this — polling is strictly informational.
    """
    start = time.monotonic()
    last_error: str | None = None
    last_done: bool = False
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
        if elapsed + interval_s > timeout_s:
            return {
                "id": job_gid,
                "done": False,
                "elapsed_s": elapsed,
                "timed_out": True,
                "error": last_error,
            }
        time.sleep(interval_s)


def _format_errors(errors) -> str:
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


def _format_one_error(err) -> str:
    if isinstance(err, dict):
        return err.get("message") or str(err)
    if isinstance(err, str):
        return err
    return str(err)


def extract_user_errors(
    result: dict,
    mutation_key: str,
    *,
    error_key: str = "userErrors",
) -> list:
    """
    Pull the userErrors list out of a mutation response, or [] if absent/null.

    Shared by every tool that needs to inspect userErrors — including callers
    that can't use `format_user_errors` because they iterate each error (e.g.
    publications.py bulk flows, media.py stage-aware reporting) or format
    non-string field paths (products.py variant bulk update).

    - `error_key` overrides the default `userErrors` slot; `priceRuleCreate`
      returns `priceRuleUserErrors` instead.
    """
    return (result.get(mutation_key) or {}).get(error_key) or []


def format_user_errors(
    result: dict,
    mutation_key: str,
    *,
    error_key: str = "userErrors",
    prefix: str = "Error",
) -> str | None:
    """
    Extract and format a mutation's userErrors payload.

    Returns an 'Error: field: message; …' string if the mutation reported
    any userErrors, else None. Callers guard with `if err: return err`.

    - `error_key` overrides the default `userErrors` slot.
    - `prefix` customizes the leading token (e.g. 'Error creating price rule').
    """
    errors = extract_user_errors(result, mutation_key, error_key=error_key)
    if not errors:
        return None
    msgs = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errors)
    return f"{prefix}: {msgs}"
