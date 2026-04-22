"""
Webhook subscription tools — list, register, and delete Shopify webhook
subscriptions on the store.

Write operations require confirm=True and log to aon_mcp_log.txt.
"""

from mcp.server.fastmcp import FastMCP
from shopify_client import ShopifyClient, to_gid, from_gid
from tools._log import log_write


LIST_WEBHOOKS = """
query ListWebhooks($first: Int!) {
  webhookSubscriptions(first: $first) {
    nodes {
      id
      topic
      format
      createdAt
      apiVersion { handle }
      endpoint {
        __typename
        ... on WebhookHttpEndpoint { callbackUrl }
      }
    }
  }
}
"""

CREATE_WEBHOOK = """
mutation CreateWebhook($topic: WebhookSubscriptionTopic!, $webhookSubscription: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
    webhookSubscription {
      id
      topic
      format
      endpoint {
        __typename
        ... on WebhookHttpEndpoint { callbackUrl }
      }
    }
    userErrors { field message }
  }
}
"""

DELETE_WEBHOOK = """
mutation DeleteWebhook($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors { field message }
  }
}
"""


def _endpoint_url(endpoint: dict) -> str:
    if not endpoint:
        return "(no endpoint)"
    return endpoint.get("callbackUrl") or f"({endpoint.get('__typename', 'unknown')})"


def register(server: FastMCP, client: ShopifyClient):

    @server.tool()
    def list_webhooks(limit: int = 50) -> str:
        """
        List active webhook subscriptions on the store.
        limit: number of subscriptions to return (max 250).
        """
        limit = min(limit, 250)
        data = client.execute(LIST_WEBHOOKS, {"first": limit})
        nodes = data.get("webhookSubscriptions", {}).get("nodes", []) or []
        if not nodes:
            return "No webhooks registered."

        lines = [f"Webhook subscriptions ({len(nodes)}):"]
        for n in nodes:
            url = _endpoint_url(n.get("endpoint"))
            api = (n.get("apiVersion") or {}).get("handle", "?")
            lines.append(
                f"  [{from_gid(n['id'])}] {n['topic']} → {url} "
                f"(format={n.get('format')}, api={api}, created={(n.get('createdAt') or '')[:10]})"
            )
        return "\n".join(lines)

    @server.tool()
    def register_webhook(
        topic: str,
        endpoint_url: str,
        message_format: str = "JSON",
        confirm: bool = False,
    ) -> str:
        """
        Register an HTTPS webhook subscription for a given topic.
        topic: Shopify webhook topic enum (e.g. ORDERS_CREATE, CHECKOUTS_CREATE).
        endpoint_url: HTTPS URL that will receive the webhook POST.
        message_format: JSON (default) or XML.
        Returns a preview unless confirm=True.
        """
        preview = (
            f"PREVIEW — Register webhook\n"
            f"  Topic    : {topic}\n"
            f"  Endpoint : {endpoint_url}\n"
            f"  Format   : {message_format}"
        )
        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        variables = {
            "topic": topic,
            "webhookSubscription": {
                "callbackUrl": endpoint_url,
                "format": message_format,
            },
        }
        result = client.execute(CREATE_WEBHOOK, variables)
        payload = result.get("webhookSubscriptionCreate", {}) or {}
        user_errors = payload.get("userErrors", []) or []
        if user_errors:
            msgs = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in user_errors)
            return f"Error: {msgs}"

        sub = payload.get("webhookSubscription") or {}
        sub_gid = sub.get("id")
        numeric_id = from_gid(sub_gid) if sub_gid else "(unknown)"

        log_write(
            "register_webhook",
            f"id={numeric_id} | topic={topic} | endpoint={endpoint_url} | format={message_format}",
        )

        return (
            f"Done. {preview}\n"
            f"  Subscription ID : {numeric_id}"
        )

    @server.tool()
    def delete_webhook(subscription_id: str, confirm: bool = False) -> str:
        """
        Delete a webhook subscription by numeric subscription id.
        Accepts either a numeric id ("123") or a full GID
        ("gid://shopify/WebhookSubscription/123").
        Returns a preview unless confirm=True.
        """
        numeric_id = from_gid(subscription_id)
        preview = (
            f"PREVIEW — Delete webhook\n"
            f"  Subscription ID : {numeric_id}"
        )
        if not confirm:
            return preview + "\n\nTo apply, call again with confirm=True."

        result = client.execute(
            DELETE_WEBHOOK,
            {"id": to_gid("WebhookSubscription", numeric_id)},
        )
        payload = result.get("webhookSubscriptionDelete", {}) or {}
        user_errors = payload.get("userErrors", []) or []
        if user_errors:
            msgs = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in user_errors)
            return f"Error: {msgs}"

        deleted_gid = payload.get("deletedWebhookSubscriptionId")
        if not deleted_gid:
            return f"Error: delete mutation returned no deletedWebhookSubscriptionId"

        log_write("delete_webhook", f"id={numeric_id}")
        return f"Done. {preview}"
