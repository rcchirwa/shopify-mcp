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
            return self._client.execute(
                gql(query_str), variable_values=variables or {}
            )
        except TransportQueryError as e:
            msgs = "; ".join(err.get("message", str(err)) for err in e.errors)
            raise RuntimeError(f"Shopify GraphQL error: {msgs}") from e
        except TransportServerError as e:
            raise RuntimeError(f"Shopify HTTP error: {e}") from e
