"""Contract test: the discounts/orders queries must validate against a recorded
snapshot of the pinned Admin GraphQL API schema (Story 9.12 / AC3, extended by
Story 9.14 / AC3 for the discount-code create mutation).

`priceRules` (QueryRoot) and `referringSite`/`landingSite` (Order) were removed
from the Admin API well before SHOPIFY_API_VERSION's documented default of
2026-01, but the queries in `shopify.queries.discounts`/`orders` kept
referencing them — nothing caught the drift until every call failed live. This
module is that catch: it runs the real queries through `graphql-core`'s
validator (already a transitive dependency of `gql`, no new package) against a
hand-trimmed schema snapshot covering only the types/fields these queries
touch, captured via live introspection against SHOPIFY_API_VERSION=2026-01 on
2026-09-14. A future PR that reintroduces a removed field, or references one
that was never there, fails this suite instead of the live store.

Story 9.14 adds the `Mutation` root and `discountCodeBasicCreate`'s input/
payload types to the same snapshot, for the same reason: `priceRuleCreate` and
`priceRuleDiscountCodeCreate` were removed alongside `priceRules`, confirmed
live against 2026-01 on 2026-09-14.

**Refreshing the snapshot.** Re-run the introspection queries in the story's
implementation notes against the then-current SHOPIFY_API_VERSION for each
type below and update the SDL. This is a deliberately narrow slice of the real
schema, not a full mirror — it only needs to grow when a query starts
selecting a field it doesn't already cover.
"""

from graphql import build_schema, parse, validate

from shopify_mcp.shopify.queries.discounts import CREATE_DISCOUNT_CODE_BASIC, GET_CODE_DISCOUNTS
from shopify_mcp.shopify.queries.orders import GET_ORDER_BY_ID, GET_ORDERS

# Trimmed Admin API SDL (2026-01), covering exactly the types/fields
# `discountNodes`, `orders`, and `order` select today.
_SCHEMA_SDL = """
scalar DateTime
scalar Decimal

type Query {
  discountNodes(first: Int, query: String): DiscountNodeConnection!
  orders(first: Int): OrderConnection!
  order(id: ID!): Order
}

type DiscountNodeConnection {
  nodes: [DiscountNode!]!
}

type DiscountNode {
  id: ID!
  discount: Discount!
}

union Discount = DiscountCodeBasic | DiscountCodeBxgy | DiscountCodeFreeShipping | DiscountCodeApp

enum DiscountStatus {
  ACTIVE
  EXPIRED
  SCHEDULED
}

type DiscountCodeBasic {
  title: String!
  status: DiscountStatus!
  endsAt: DateTime
  usageLimit: Int
  codes(first: Int): DiscountRedeemCodeConnection!
  customerGets: DiscountCustomerGets!
}

type DiscountCodeBxgy {
  title: String!
  status: DiscountStatus!
  endsAt: DateTime
  usageLimit: Int
  codes(first: Int): DiscountRedeemCodeConnection!
}

type DiscountCodeFreeShipping {
  title: String!
  status: DiscountStatus!
  endsAt: DateTime
  usageLimit: Int
  codes(first: Int): DiscountRedeemCodeConnection!
}

type DiscountCodeApp {
  title: String!
  status: DiscountStatus!
  endsAt: DateTime
  usageLimit: Int
  codes(first: Int): DiscountRedeemCodeConnection!
}

type DiscountRedeemCodeConnection {
  nodes: [DiscountRedeemCode!]!
  pageInfo: PageInfo!
}

type DiscountRedeemCode {
  code: String!
}

type DiscountCustomerGets {
  value: DiscountCustomerGetsValue!
}

union DiscountCustomerGetsValue = DiscountPercentage | DiscountAmount

type DiscountPercentage {
  percentage: Float!
}

type DiscountAmount {
  amount: MoneyV2!
}

type OrderConnection {
  nodes: [Order!]!
}

type MoneyBag {
  shopMoney: MoneyV2!
}

type MoneyV2 {
  amount: Decimal!
}

type PageInfo {
  hasNextPage: Boolean!
  endCursor: String
}

type LineItemConnection {
  nodes: [LineItem!]!
  pageInfo: PageInfo!
}

type LineItem {
  name: String!
  quantity: Int!
  originalUnitPriceSet: MoneyBag
}

enum OrderDisplayFinancialStatus {
  PAID
}

enum OrderDisplayFulfillmentStatus {
  FULFILLED
}

type CustomerVisit {
  source: String
  referrerUrl: String
  utmParameters: UTMParameters
}

type UTMParameters {
  source: String
  medium: String
  campaign: String
}

type CustomerJourneySummary {
  firstVisit: CustomerVisit
  lastVisit: CustomerVisit
}

type Order {
  id: ID!
  name: String!
  createdAt: DateTime!
  totalPriceSet: MoneyBag
  lineItems(first: Int!, after: String): LineItemConnection!
  customerJourneySummary: CustomerJourneySummary
  displayFinancialStatus: OrderDisplayFinancialStatus
  displayFulfillmentStatus: OrderDisplayFulfillmentStatus
}

type Mutation {
  discountCodeBasicCreate(basicCodeDiscount: DiscountCodeBasicInput!): DiscountCodeBasicCreatePayload!
}

input DiscountCodeBasicInput {
  title: String
  code: String
  startsAt: DateTime
  endsAt: DateTime
  usageLimit: Int
  appliesOncePerCustomer: Boolean
  context: DiscountContextInput
  customerGets: DiscountCustomerGetsInput
  combinesWith: DiscountCombinesWithInput
}

input DiscountContextInput {
  all: DiscountBuyerSelection
}

enum DiscountBuyerSelection {
  ALL
}

input DiscountCustomerGetsInput {
  value: DiscountCustomerGetsValueInput
  items: DiscountItemsInput
}

input DiscountCustomerGetsValueInput {
  percentage: Float
}

input DiscountItemsInput {
  all: Boolean
}

input DiscountCombinesWithInput {
  productDiscounts: Boolean
  orderDiscounts: Boolean
  shippingDiscounts: Boolean
}

type DiscountCodeBasicCreatePayload {
  codeDiscountNode: DiscountCodeNode
  userErrors: [DiscountUserError!]!
}

type DiscountCodeNode {
  id: ID!
}

type DiscountUserError {
  field: [String!]
  message: String!
  code: DiscountErrorCode
}

enum DiscountErrorCode {
  INVALID
}
"""

_SCHEMA = build_schema(_SCHEMA_SDL)


def _assert_valid(query_str: str) -> None:
    errors = validate(_SCHEMA, parse(query_str))
    assert errors == [], f"query failed schema validation: {errors}"


def test_get_code_discounts_query_matches_pinned_schema():
    _assert_valid(GET_CODE_DISCOUNTS)


def test_get_orders_query_matches_pinned_schema():
    _assert_valid(GET_ORDERS)


def test_get_order_by_id_query_matches_pinned_schema():
    _assert_valid(GET_ORDER_BY_ID)


def test_create_discount_code_basic_mutation_matches_pinned_schema():
    _assert_valid(CREATE_DISCOUNT_CODE_BASIC)


def test_validator_catches_a_field_removed_from_the_schema():
    """Proves the harness actually catches a removed field (not a no-op check):
    `priceRules` doesn't exist on QueryRoot in the recorded schema (renamed
    `Query` here) — this is the exact shape of bug this suite exists to catch."""
    bad_query = "query Bad($first: Int!) { priceRules(first: $first) { nodes { id } } }"
    errors = validate(_SCHEMA, parse(bad_query))
    assert errors != []
    assert any("priceRules" in str(e) for e in errors)


def test_validator_catches_referring_site_removed_from_order():
    bad_query = "query Bad { orders(first: 1) { nodes { referringSite } } }"
    errors = validate(_SCHEMA, parse(bad_query))
    assert errors != []
    assert any("referringSite" in str(e) for e in errors)


def test_validator_catches_price_rule_create_removed_from_schema():
    """Same shape of bug, mutation side: `priceRuleCreate` doesn't exist in the
    recorded schema — this is exactly what broke `create_discount_code` before
    Story 9.14 replaced it with `discountCodeBasicCreate`."""
    bad_mutation = "mutation Bad { priceRuleCreate(priceRule: {}) { priceRule { id } } }"
    errors = validate(_SCHEMA, parse(bad_mutation))
    assert errors != []
    assert any("priceRuleCreate" in str(e) for e in errors)
