"""
Webhook subscription tools — list, register, and delete Shopify webhook
subscriptions on the store.

Write operations require confirm=True and log to aon_mcp_log.txt.
"""

from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from shopify_client import ShopifyClient
from tools._gid import from_gid, to_gid
from tools._write_tool import write_gate

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


_EXTERNAL_DOMAIN_WARNING = (
    "⚠ EXTERNAL DOMAIN — verify this is the intended receiver before confirming.\n"
)


def _check_endpoint(endpoint_url: str, client: ShopifyClient) -> tuple:
    """
    Returns (allowed, annotation).
    allowed=False: return annotation as an error.
    allowed=True, annotation non-empty: prepend as warning to preview.
    allowed=True, annotation="": hostname is in allowlist; proceed silently.
    """
    hostname = (urlparse(endpoint_url).hostname or "").lower()
    allowlist = client._settings.webhook_allowlist_set
    if allowlist:
        if hostname not in allowlist:
            return False, (
                f"Error: endpoint hostname '{hostname}' is not in WEBHOOK_ALLOWLIST_HOSTS. "
                f"Add it to the env var to permit this receiver."
            )
        return True, ""
    return True, _EXTERNAL_DOMAIN_WARNING


def _endpoint_url(endpoint: dict) -> str:
    if not endpoint:
        return "(no endpoint)"
    return endpoint.get("callbackUrl") or f"({endpoint.get('__typename', 'unknown')})"


def register(server: FastMCP, client: ShopifyClient) -> None:

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
        allowed, annotation = _check_endpoint(endpoint_url, client)
        if not allowed:
            return annotation

        preview = (
            f"{annotation}"
            f"PREVIEW — Register webhook\n"
            f"  Topic    : {topic}\n"
            f"  Endpoint : {endpoint_url}\n"
            f"  Format   : {message_format}"
        )
        variables = {
            "topic": topic,
            "webhookSubscription": {
                "callbackUrl": endpoint_url,
                "format": message_format,
            },
        }
        captured: dict = {}

        def _execute() -> dict:
            result = client.execute(CREATE_WEBHOOK, variables)
            captured.update(result or {})
            return result

        def _numeric_id() -> str:
            sub = (captured.get("webhookSubscriptionCreate") or {}).get("webhookSubscription") or {}
            sub_gid = sub.get("id")
            return from_gid(sub_gid) if sub_gid else "(unknown)"

        return write_gate(
            preview=preview,
            confirm=confirm,
            execute=_execute,
            mutation_key="webhookSubscriptionCreate",
            log_name="register_webhook",
            log_description=lambda: (
                f"id={_numeric_id()} | topic={topic} | endpoint={endpoint_url} | format={message_format}"
            ),
            done_text=lambda: f"Done. {preview}\n  Subscription ID : {_numeric_id()}",
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
        preview = f"PREVIEW — Delete webhook\n  Subscription ID : {numeric_id}"

        def _check_deleted(result: dict) -> str | None:
            payload = result.get("webhookSubscriptionDelete", {}) or {}
            if not payload.get("deletedWebhookSubscriptionId"):
                return "Error: delete mutation returned no deletedWebhookSubscriptionId"
            return None

        return write_gate(
            preview=preview,
            confirm=confirm,
            execute=lambda: client.execute(
                DELETE_WEBHOOK,
                {"id": to_gid("WebhookSubscription", numeric_id)},
            ),
            mutation_key="webhookSubscriptionDelete",
            log_name="delete_webhook",
            log_description=f"id={numeric_id}",
            post_execute_check=_check_deleted,
        )
