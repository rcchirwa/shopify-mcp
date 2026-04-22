"""
Shopify Admin GraphQL API wrapper.
Loads credentials from .env — never hardcode secrets here.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from gql import gql, Client
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.exceptions import TransportServerError, TransportQueryError

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
    return gid.split("/")[-1]


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
        # 2024-10: current stable. `InventoryLevel.available` was removed in
        # 2024-07 in favor of `quantities(names: ["available"])`, so older
        # pins will fail the inventory queries.
        api_version = os.getenv("SHOPIFY_API_VERSION", "2024-10")

        if not store_url or not access_token:
            raise ValueError(
                "SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN must be set in .env"
            )

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

    def execute(self, query_str: str, variables: dict = None) -> dict:
        try:
            result = self._client.execute(
                gql(query_str), variable_values=variables or {}
            )
        except TransportQueryError as e:
            raise RuntimeError(f"Shopify GraphQL error: {_format_errors(e.errors)}") from e
        except TransportServerError as e:
            raise RuntimeError(f"Shopify HTTP error: {str(e)}") from e

        if not isinstance(result, dict):
            # Surface the real payload (scope error text, HTML error page, etc.)
            # so callers don't crash downstream with 'str' object has no attribute 'get'.
            preview = str(result)[:500]
            raise RuntimeError(
                f"Shopify returned non-dict response "
                f"(type={type(result).__name__}): {preview}"
            )
        return result


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
