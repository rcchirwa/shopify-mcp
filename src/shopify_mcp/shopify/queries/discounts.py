"""GraphQL query/mutation strings for the ``discounts`` domain.

The bottom layer of the ``discounts`` migration (Story 10.27 / A5, following the
products pilot in Story 10.23 and catalog_hygiene in Story 10.25). Pure strings —
no imports from ``shopify.operations`` or ``tools``.

**No shared fragment applies.** discounts has no by-id/by-handle read pair (just a
code-discounts list read plus the two-step price-rule / discount-code create
mutations), and the selection sets do not overlap, so there is no duplicated
selection set to factor out. Fragment dedup is opportunistic (Story 10.27 / A5,
AC3) and is deliberately not forced here.

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

**Deliberately out of scope.** `CREATE_PRICE_RULE`/`CREATE_DISCOUNT_CODE`
(`priceRuleCreate`/`priceRuleDiscountCodeCreate`) are equally broken on 2026-01
— confirmed live, neither mutation exists any more — but `create_discount_code`
was never called live and carries no failing AC in this story; fixing it is
scoped separately.
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

CREATE_PRICE_RULE = """
mutation CreatePriceRule($input: PriceRuleInput!) {
  priceRuleCreate(priceRule: $input) {
    priceRule { id }
    priceRuleUserErrors { field message }
  }
}
"""

CREATE_DISCOUNT_CODE = """
mutation CreateDiscountCode($priceRuleId: ID!, $code: String!) {
  priceRuleDiscountCodeCreate(priceRuleId: $priceRuleId, code: $code) {
    priceRuleDiscountCode { code }
    userErrors { field message }
  }
}
"""
