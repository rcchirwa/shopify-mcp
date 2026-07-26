"""GraphQL query/mutation strings for the ``discounts`` domain.

The bottom layer of the ``discounts`` migration (Story 10.27 / A5, following the
products pilot in Story 10.23 and catalog_hygiene in Story 10.25). Pure strings —
no imports from ``shopify.operations`` or ``tools``.

**No shared fragment applies.** discounts has no by-id/by-handle read pair (just a
price-rule list read plus the two-step price-rule / discount-code create
mutations), and the three selection sets do not overlap, so there is no
duplicated selection set to factor out. Fragment dedup is opportunistic (Story
10.27 / A5, AC3) and is deliberately not forced here.
"""

GET_PRICE_RULES = """
query GetPriceRules($first: Int!) {
  priceRules(first: $first) {
    nodes {
      id
      title
      valueType
      value
      usageLimit
      endsAt
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
