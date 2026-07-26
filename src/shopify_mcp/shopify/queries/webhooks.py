"""GraphQL query/mutation strings for the ``webhooks`` domain.

The bottom layer of the ``webhooks`` migration (Story 10.31 / A5 — the last domain,
closing A5; following the products pilot in Story 10.23 and the catalog_hygiene /
collections / discounts / inventory / orders / publications migrations). Pure
strings — no imports from ``shopify.operations`` or ``tools``.

**No shared fragment applies.** webhooks has no by-id/by-handle read pair — just a
single list read (``LIST_WEBHOOKS``) plus the create/delete mutations
(``CREATE_WEBHOOK``, ``DELETE_WEBHOOK``), the same list-plus-mutations shape as
discounts (Story 10.27), where none applied either. The only selection that recurs
is the small ``endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } }``
union block shared between the list read and the create mutation, but that mirrors
the recurring ``{ shopMoney { amount } }`` money block orders left un-factored
(Story 10.29): the fragment pattern in this codebase centralizes an entity's shared
*core* across a *read pair*, not a micro sub-selection spanning a read and a
mutation. Forcing a fragment here would be the anti-pattern AC3 warns against, so
none is extracted (Story 10.31 / A5, AC3).
"""

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
