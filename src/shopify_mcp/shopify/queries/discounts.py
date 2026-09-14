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
whole-number `PriceRuleInput.value` the old shape used), and there is no
`customerSelection` field at all, because a code-based discount is already
gated by whoever holds the code, not a customer segment.

`DiscountContextInput` (the `context` field) is schema-nullable but
**business-logic required** — confirmed live, 2026-09-14: omitting it fails
with "Context can't be blank", a class of requirement introspection cannot
show (see `tools.discounts` for the `{all: ALL}` value this tool sends).
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
          codes(first: $codesFirst) { nodes { code } pageInfo { hasNextPage } }
        }
        ... on DiscountCodeFreeShipping {
          title
          status
          endsAt
          usageLimit
          codes(first: $codesFirst) { nodes { code } pageInfo { hasNextPage } }
        }
        ... on DiscountCodeApp {
          title
          status
          endsAt
          usageLimit
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
