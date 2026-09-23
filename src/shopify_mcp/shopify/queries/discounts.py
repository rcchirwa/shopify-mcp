"""GraphQL query/mutation strings for the ``discounts`` domain.

The bottom layer of the ``discounts`` migration (Story 10.27 / A5, following the
products pilot in Story 10.23 and catalog_hygiene in Story 10.25). Pure strings —
no imports from ``shopify.operations`` or ``tools``.

**No shared fragment applies.** discounts has no by-id/by-handle read pair (just a
code-discounts list read plus the discount-code create mutation), and the
selection sets do not overlap, so there is no duplicated selection set to
factor out. Fragment dedup is opportunistic (Story 10.27 / A5, AC3) and is
deliberately not forced here.

**GET_CODE_DISCOUNTS replaced GET_PRICE_RULES (Story 9.12).** `priceRules` and
the whole PriceRule API were removed from the Admin GraphQL API before
SHOPIFY_API_VERSION's documented default of 2026-01 — every call failed with
"Field 'priceRules' doesn't exist on type 'QueryRoot'". The replacement,
`discountNodes`, returns a `Discount` **union** (confirmed live against
2026-01, 2026-09-14): `codeDiscountNodes`, the field name a first pass at this
story assumed, does not exist either. Selecting fields off a union requires an
inline fragment per member type even where the field name and shape are
identical across members — plain GraphQL, not a Shopify quirk — hence the four
`... on DiscountCode*` blocks below rather than one shared selection.
`query: "method:code"` filters the connection to code discounts only (as
opposed to automatic discounts, which this tool has never listed).

**CREATE_DISCOUNT_CODE_BASIC replaced CREATE_PRICE_RULE + CREATE_DISCOUNT_CODE
(Story 9.14).** `priceRuleCreate` and `priceRuleDiscountCodeCreate` are gone
from the Admin API — confirmed live against 2026-01, 2026-09-14, alongside
`priceRules` (see the note above). The replacement,
`discountCodeBasicCreate`, folds the old two-step price-rule-then-attach-code
flow into a single mutation: `DiscountCodeBasicInput` carries the code and the
percentage directly (`customerGets.value.percentage`, a 0-1 fraction — not the
whole-number `PriceRuleInput.value` the old shape used). **Correction,
2026-09-23 (Story 9.17 review):** this paragraph previously claimed
`DiscountCodeBasicInput` had "no `customerSelection` field at all, because a
code-based discount is already gated by whoever holds the code, not a
customer segment." Both halves were wrong. The input DOES declare a
`customerSelection` field — deprecated in favor of `context` (the field this
mutation sends, below) — and a code-based discount can absolutely be
segment- or customer-gated on top of holding the code: confirmed live against
2026-01, e.g. `AON_DAY_ONE_VIP` and `VIPFOUNDERS20`, both restricted to the
"AON Founders VIP List" segment. That gap is exactly what Story 9.17's read
side (below) now surfaces.

`DiscountContextInput` (the `context` field) is schema-nullable but
**business-logic required** — confirmed live, 2026-09-14: omitting it fails
with "Context can't be blank", a class of requirement introspection cannot
show (see `tools.discounts` for the `{all: ALL}` value this tool sends).

**GET_CODE_DISCOUNTS also reads eligibility now (Story 9.17).** Before this,
the read side reported a code's terms (status, usage limit, expiry) but never
WHO may redeem it, which read a single-customer or segment-gated code as an
unlimited code open to anyone. The output `context` field (confirmed live
against 2026-01, 2026-09-23) is a `DiscountContext` union —
`DiscountBuyerSelectionAll | DiscountCustomerSegments | DiscountCustomers` —
selected alongside `appliesOncePerCustomer` on all four `Discount` union
members (see docs/tech-debt.md, Story 9.17, for why all four, not just
`DiscountCodeBasic`). This is a read-side companion to the `context` INPUT
field the create mutation already sends above; they are different things with
the same name — one is what a new code is created with, the other is what an
existing code turns out to have. Only `customers { id }` is selected, never
`email` (Customer.email is itself deprecated) — data that is never fetched
cannot leak. The deprecated `customerSelection` OUTPUT field on
`DiscountCodeBasic` (distinct from the `DiscountCodeBasicInput.customerSelection`
input field noted above) is deliberately not selected here in its place.
"""

GET_CODE_DISCOUNTS = """
query GetCodeDiscounts($first: Int!, $query: String, $codesFirst: Int!) {
  discountNodes(first: $first, query: $query) {
    nodes {
      id
      discount {
        __typename
        ... on DiscountCodeBasic {
          title
          status
          endsAt
          usageLimit
          appliesOncePerCustomer
          context {
            __typename
            ... on DiscountBuyerSelectionAll { all }
            ... on DiscountCustomers { customers { id } }
            ... on DiscountCustomerSegments { segments { id name } }
          }
          codes(first: $codesFirst) { nodes { code } pageInfo { hasNextPage } }
          customerGets {
            value {
              __typename
              ... on DiscountPercentage { percentage }
              ... on DiscountAmount { amount { amount } }
            }
          }
        }
        ... on DiscountCodeBxgy {
          title
          status
          endsAt
          usageLimit
          appliesOncePerCustomer
          context {
            __typename
            ... on DiscountBuyerSelectionAll { all }
            ... on DiscountCustomers { customers { id } }
            ... on DiscountCustomerSegments { segments { id name } }
          }
          codes(first: $codesFirst) { nodes { code } pageInfo { hasNextPage } }
        }
        ... on DiscountCodeFreeShipping {
          title
          status
          endsAt
          usageLimit
          appliesOncePerCustomer
          context {
            __typename
            ... on DiscountBuyerSelectionAll { all }
            ... on DiscountCustomers { customers { id } }
            ... on DiscountCustomerSegments { segments { id name } }
          }
          codes(first: $codesFirst) { nodes { code } pageInfo { hasNextPage } }
        }
        ... on DiscountCodeApp {
          title
          status
          endsAt
          usageLimit
          appliesOncePerCustomer
          context {
            __typename
            ... on DiscountBuyerSelectionAll { all }
            ... on DiscountCustomers { customers { id } }
            ... on DiscountCustomerSegments { segments { id name } }
          }
          codes(first: $codesFirst) { nodes { code } pageInfo { hasNextPage } }
        }
      }
    }
  }
}
"""

CREATE_DISCOUNT_CODE_BASIC = """
mutation CreateDiscountCodeBasic($input: DiscountCodeBasicInput!) {
  discountCodeBasicCreate(basicCodeDiscount: $input) {
    codeDiscountNode { id }
    userErrors { field message code }
  }
}
"""
