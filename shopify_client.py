"""
Shopify Admin GraphQL API wrapper.
Loads credentials from .env — never hardcode secrets here.
"""

import os
from dotenv import load_dotenv
from gql import gql, Client
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.exceptions import TransportServerError, TransportQueryError

load_dotenv()

STORE_URL = os.getenv("SHOPIFY_STORE_URL")
ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")


def to_gid(resource_type: str, numeric_id) -> str:
    return f"gid://shopify/{resource_type}/{numeric_id}"


def from_gid(gid: str) -> str:
    return gid.split("/")[-1]


class ShopifyClient:
    def __init__(self):
        if not STORE_URL or not ACCESS_TOKEN:
            raise ValueError(
                "SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN must be set in .env"
            )
        transport = RequestsHTTPTransport(
            url=f"https://{STORE_URL}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": ACCESS_TOKEN},
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
