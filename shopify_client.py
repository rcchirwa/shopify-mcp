"""
Shopify Admin GraphQL API wrapper.
Loads credentials from .env — never hardcode secrets here.
"""

import os
from dotenv import load_dotenv
from gql import gql, Client
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.exceptions import TransportServerError, TransportQueryError

def to_gid(resource_type: str, numeric_id) -> str:
    return f"gid://shopify/{resource_type}/{numeric_id}"


def from_gid(gid: str) -> str:
    return gid.split("/")[-1]


class ShopifyClient:
    def __init__(self):
        load_dotenv()
        store_url = os.getenv("SHOPIFY_STORE_URL")
        access_token = os.getenv("SHOPIFY_ACCESS_TOKEN")
        api_version = os.getenv("SHOPIFY_API_VERSION", "2024-01")

        if not store_url or not access_token:
            raise ValueError(
                "SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN must be set in .env"
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
