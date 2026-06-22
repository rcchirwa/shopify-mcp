"""Typed webhooks operations — data access over ``shopify.queries.webhooks``.

Each function takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and performs GID coercion + GraphQL-variable building + query/mutation execution,
returning structured data (the list read) or the raw Shopify response (writes). No
MCP imports and no output formatting, so these are callable from non-MCP entry
points (CLI, scripts, tests) — Story 10.31 / A5, AC4. ``tools/webhooks.py`` layers
the endpoint-allowlist validation, the preview/confirm flow, audit logging, and
string formatting on top.
"""

from typing import Any

from shopify._client import GraphQLClient
from shopify._ids import from_gid, to_gid
from shopify.queries.webhooks import CREATE_WEBHOOK, DELETE_WEBHOOK, LIST_WEBHOOKS


def read_webhooks(client: GraphQLClient, first: int) -> list[dict[str, Any]]:
    """List webhook subscriptions, returning the node list.

    ``first`` is the already-clamped count the tool passes through (≤ 250); the
    operation does no clamping of its own. A ``nodes: null`` / missing-connection
    response yields ``[]`` rather than ``None``."""
    data = client.execute(LIST_WEBHOOKS, {"first": first})
    return data.get("webhookSubscriptions", {}).get("nodes", []) or []


def create_webhook(
    client: GraphQLClient, topic: str, endpoint_url: str, message_format: str
) -> dict[str, Any]:
    """Execute ``webhookSubscriptionCreate`` for an HTTPS endpoint.

    Builds the ``WebhookSubscriptionInput`` (``callbackUrl`` + ``format``) from the
    endpoint URL and message format, and returns the raw mutation result for the
    tool layer to map userErrors / surface the new subscription id."""
    variables = {
        "topic": topic,
        "webhookSubscription": {
            "callbackUrl": endpoint_url,
            "format": message_format,
        },
    }
    return client.execute(CREATE_WEBHOOK, variables)


def delete_webhook(client: GraphQLClient, subscription_id: str) -> dict[str, Any]:
    """Execute ``webhookSubscriptionDelete`` by subscription id.

    Accepts a numeric id (``"123"``) or a full GID
    (``gid://shopify/WebhookSubscription/123``) and coerces it to the canonical
    WebhookSubscription GID (``from_gid`` strips, ``to_gid`` rebuilds — no
    double-prefix) before executing. Returns the raw mutation result."""
    gid = to_gid("WebhookSubscription", from_gid(subscription_id))
    return client.execute(DELETE_WEBHOOK, {"id": gid})
