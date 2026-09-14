"""GraphQL query strings for the ``orders`` domain.

The bottom layer of the ``orders`` migration (Story 10.29 / A5, following the
products pilot in Story 10.23 and the catalog_hygiene / collections / discounts /
inventory migrations). Pure strings — no imports from ``shopify.operations`` or
``tools``. ``orders`` is a read-only domain: two reads, no mutations.

**A shared fragment applies.** The two reads — ``GET_ORDERS`` (list) and
``GET_ORDER_BY_ID`` (single) — select the same order-node core
(``id name createdAt totalPriceSet { shopMoney { amount } }``) verbatim, so it is
factored into the ``OrderCoreFields`` fragment both queries spread (Story 10.29 /
A5, AC3). Each read still adds its own fields inline — the list read adds a fixed
``lineItems(first: $lineItemsFirst)`` summary (with ``pageInfo.hasNextPage`` so the
per-order cap is detected and warned, not silently dropped — Story 10.34 / A3);
the single read adds the financial/fulfillment status and a paginated
``lineItems`` with unit prices. Centralizing only the shared core (which
includes the version-sensitive ``totalPriceSet`` money shape) means the next
Admin-API money-shape change is a one-line edit instead of two.

**Traffic source moved to ``customerJourneySummary`` (Story 9.12).**
``referringSite``/``landingSite`` were removed from ``Order`` before
SHOPIFY_API_VERSION's documented default of 2026-01 — every ``get_orders`` /
``get_order`` call failed with "Field 'referringSite' doesn't exist on type
'Order'". The replacement lives under ``customerJourneySummary.firstVisit`` /
``.lastVisit`` (each a ``CustomerVisit``, confirmed live against 2026-01,
2026-09-14), which also carries ``utmParameters`` — data the old fields never
had, and that GA4 UTM stitching depends on. The list read selects both
``firstVisit`` and ``lastVisit`` (approach 2 from the story: first/last touch
+ UTM); the single read selects only ``lastVisit``, the closest analog to the
old single ``referringSite`` field it replaces.
"""

# Shared core selection on an Order: identity + creation date + total money.
# `totalPriceSet { shopMoney { amount } }` is the version-sensitive bit (the
# 2024-07+ Money shape), so centralizing it keeps the next shape change to a
# single edit; both reads extract the amount via the same null-tolerant chain.
ORDER_CORE_FIELDS = """
fragment OrderCoreFields on Order {
  id
  name
  createdAt
  totalPriceSet { shopMoney { amount } }
}
"""

# NOTE: orders.nodes.lineItems is a connection nested inside a list connection
# (orders is itself paginated). client.paginate() walks a single top-level
# connection and cannot paginate a nested connection, so the per-order line items
# stay capped at $lineItemsFirst below (sourced from GET_ORDERS_LINE_ITEM_CAP in
# shopify.operations.orders) — full pagination of this nested-in-list connection
# remains out of scope. We DO select pageInfo { hasNextPage } so the cap is
# detected, not silent: get_orders emits a per-order at-cap WARNING when an order is
# truncated (parity with the single-order get_order path; Story 10.34 / A3 —
# warn-on-cap, mirroring T-9.5-variants-cap / Story 10.12-media-cap).
GET_ORDERS = (
    ORDER_CORE_FIELDS
    + """
query GetOrders($first: Int!, $lineItemsFirst: Int!) {
  orders(first: $first) {
    nodes {
      ...OrderCoreFields
      lineItems(first: $lineItemsFirst) {
        nodes {
          name
          quantity
        }
        pageInfo { hasNextPage }
      }
      customerJourneySummary {
        firstVisit { source referrerUrl utmParameters { source medium campaign } }
        lastVisit { source referrerUrl utmParameters { source medium campaign } }
      }
    }
  }
}
"""
)

GET_ORDER_BY_ID = (
    ORDER_CORE_FIELDS
    + """
query GetOrderById($id: ID!, $first: Int!, $after: String) {
  order(id: $id) {
    ...OrderCoreFields
    displayFinancialStatus
    displayFulfillmentStatus
    customerJourneySummary {
      lastVisit { source referrerUrl utmParameters { source medium campaign } }
    }
    lineItems(first: $first, after: $after) {
      nodes {
        name
        quantity
        originalUnitPriceSet { shopMoney { amount } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
)
