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

Story 9.18 adds `productUpdate`, `QueryRoot.job`/`Job`, and
`webhookSubscriptionCreate` — plus, for the first time, a **second leg**.

**Why a second leg (Story 9.18).** `graphql.validate` reads the DOCUMENT only.
An input object supplied as a *variable* is opaque to it, so a document can
validate perfectly while the payload the code actually sends is rejected live.
That is not hypothetical: `CREATE_WEBHOOK` validated clean throughout the
period `register_webhook` was dead, because the removed field
(`WebhookSubscriptionInput.callbackUrl`, now `uri`) lived in the variable. No
amount of document validation could ever have seen it. So the suite now also
calls the real `shopify.operations` functions, captures the `(query,
variables)` pairs they emit, and coerces those variables against this schema
with `graphql.execution.values.get_variable_values`. Input objects are declared
with their REAL field sets rather than placeholders — a placeholder would make
the coercion leg accept anything — and negative tests pin that it discriminates.

**Provenance.** The Story 9.18 types were transcribed from live introspection
captured 2026-09-15 against SHOPIFY_API_VERSION=2026-01, where the SERVED
version was read positively off the `X-Shopify-Api-Version` response header
(the header Story 9.13 surfaces) and asserted equal to the requested version
*before* anything was copied out of the result. That ordering matters: Shopify
answers HTTP 200 on a substituted version rather than rejecting an unsupported
pin, so an unasserted probe yields a confident, wrong answer — and the absence
of a drift warning is not evidence, since a missing header is silent too.

**Refreshing the snapshot.** Re-run the introspection queries in the story's
implementation notes against the then-current SHOPIFY_API_VERSION for each
type below and update the SDL. This is a deliberately narrow slice of the real
schema, not a full mirror — it only needs to grow when a query starts
selecting a field it doesn't already cover.
"""

import pytest
from graphql import build_schema, parse, validate
from graphql.execution.values import get_variable_values

from shopify_mcp.client import JOB_STATUS_QUERY
from shopify_mcp.shopify.operations import products as ops
from shopify_mcp.shopify.operations import webhooks as webhook_ops
from shopify_mcp.shopify.queries.discounts import CREATE_DISCOUNT_CODE_BASIC, GET_CODE_DISCOUNTS
from shopify_mcp.shopify.queries.orders import GET_ORDER_BY_ID, GET_ORDERS
from shopify_mcp.shopify.queries.products import (
    UPDATE_PRODUCT,
    UPDATE_PRODUCT_STATUS,
    UPDATE_PRODUCT_TAGS,
)
from shopify_mcp.shopify.queries.webhooks import CREATE_WEBHOOK

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

# ---------------------------------------------------------------------------
# Story 9.18 slice. Every type below was transcribed from a live introspection
# on 2026-09-15 whose SERVED X-Shopify-Api-Version header was positively READ
# and asserted equal to the requested 2026-01 before anything was copied out of
# it (see the module docstring). The input objects carry their REAL field sets,
# not placeholders — a placeholder would make the coercion leg accept anything
# and prove nothing.
# ---------------------------------------------------------------------------

scalar URL

enum ProductStatus {
  ACTIVE
  ARCHIVED
  DRAFT
  UNLISTED
}

enum MediaContentType {
  VIDEO
  EXTERNAL_VIDEO
  MODEL_3D
  IMAGE
}

enum WebhookSubscriptionFormat {
  JSON
  XML
}

# Deliberately one value: WebhookSubscriptionTopic has hundreds, and this slice
# only needs the topic the contract tests actually send.
enum WebhookSubscriptionTopic {
  ORDERS_CREATE
}

input SEOInput {
  title: String
  description: String
}

input MetafieldInput {
  id: ID
  namespace: String
  key: String
  value: String
  type: String
}

input HasMetafieldsMetafieldIdentifierInput {
  namespace: String
  key: String!
}

input CreateMediaInput {
  originalSource: String!
  alt: String
  mediaContentType: MediaContentType!
}

input ProductUpdateInput {
  descriptionHtml: String
  handle: String
  seo: SEOInput
  productType: String
  tags: [String!]
  templateSuffix: String
  giftCardTemplateSuffix: String
  title: String
  vendor: String
  category: ID
  redirectNewHandle: Boolean
  id: ID
  collectionsToJoin: [ID!]
  collectionsToLeave: [ID!]
  deleteConflictingConstrainedMetafields: Boolean
  metafields: [MetafieldInput!]
  status: ProductStatus
  requiresSellingPlan: Boolean
}

input WebhookSubscriptionInput {
  format: WebhookSubscriptionFormat
  includeFields: [String!]
  filter: String
  metafieldNamespaces: [String!]
  metafields: [HasMetafieldsMetafieldIdentifierInput!]
  uri: String
}

type UserError {
  field: [String!]
  message: String!
}

type Product {
  id: ID!
  title: String!
  handle: String!
  tags: [String!]!
  status: ProductStatus!
}

type ProductUpdatePayload {
  product: Product
  userErrors: [UserError!]!
}

type Job {
  id: ID!
  done: Boolean!
}

type WebhookHttpEndpoint {
  callbackUrl: URL!
}

type WebhookEventBridgeEndpoint {
  arn: String!
}

type WebhookPubSubEndpoint {
  pubSubProject: String!
}

union WebhookSubscriptionEndpoint =
    WebhookHttpEndpoint
  | WebhookEventBridgeEndpoint
  | WebhookPubSubEndpoint

type WebhookSubscription {
  id: ID!
  topic: WebhookSubscriptionTopic!
  format: WebhookSubscriptionFormat!
  endpoint: WebhookSubscriptionEndpoint
}

type WebhookSubscriptionCreatePayload {
  webhookSubscription: WebhookSubscription
  userErrors: [UserError!]!
}

extend type Query {
  job(id: ID!): Job
}

extend type Mutation {
  productUpdate(product: ProductUpdateInput, media: [CreateMediaInput!]): ProductUpdatePayload
  webhookSubscriptionCreate(
    topic: WebhookSubscriptionTopic!
    webhookSubscription: WebhookSubscriptionInput!
  ): WebhookSubscriptionCreatePayload
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


# ===========================================================================
# Story 9.18 — LEG (a): document validation
# ===========================================================================


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(UPDATE_PRODUCT, id="UPDATE_PRODUCT"),
        pytest.param(UPDATE_PRODUCT_TAGS, id="UPDATE_PRODUCT_TAGS"),
        pytest.param(UPDATE_PRODUCT_STATUS, id="UPDATE_PRODUCT_STATUS"),
        pytest.param(JOB_STATUS_QUERY, id="JOB_STATUS_QUERY"),
        pytest.param(CREATE_WEBHOOK, id="CREATE_WEBHOOK"),
    ],
)
def test_story_9_18_documents_validate_against_pinned_schema(document):
    _assert_valid(document)


def test_validator_catches_product_update_input_argument():
    """`productUpdate(input:)` was removed by 2026-01 — the only arguments are
    `product` and `media`. Three constants in `queries/products.py` still sent
    `input:` and every one of the five product-write tools was dead live."""
    bad = "mutation Bad($input: ProductUpdateInput!) { productUpdate(input: $input) { product { id } } }"
    errors = validate(_SCHEMA, parse(bad))
    assert errors != []
    assert any("input" in str(e) for e in errors)


def test_validator_catches_job_spread_inside_node():
    """`Job` implements no interfaces on 2026-01, so it is not a `Node` and the
    old `node(id:) { ... on Job }` shape can never match. Recorded here as the
    schema slice declares no `node` field at all, which is the same catch."""
    bad = "query Bad($id: ID!) { node(id: $id) { ... on Job { id done } } }"
    errors = validate(_SCHEMA, parse(bad))
    assert errors != []


# ===========================================================================
# Story 9.18 — LEG (b): variable coercion
#
# This is the leg that actually matters, and the reason this story exists.
# `graphql.validate` reads the DOCUMENT; an input object supplied as a variable
# is opaque to it. `CREATE_WEBHOOK` validated perfectly clean above while
# `register_webhook` was dead live, because the removed field (`callbackUrl`)
# lived in the variable, not the query text. Coercing the payloads the
# operations layer actually emits is the only check that can see that class of
# break — so these tests call the real functions and coerce what comes out.
# ===========================================================================


class _CapturingClient:
    """Minimal GraphQLClient stand-in that records the (query, variables) sent."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, variables=None):
        self.calls.append((query, variables or {}))
        return {}


def _emit(operation, *args, **kwargs) -> tuple[str, dict]:
    """Run a real operations function and return the payload it emitted."""
    client = _CapturingClient()
    operation(client, *args, **kwargs)
    assert len(client.calls) == 1, f"expected exactly one execute(), got {len(client.calls)}"
    return client.calls[0]


def _coerce(query: str, variables: dict):
    """Coerce `variables` against `query`'s variable definitions.

    graphql-core 3.2 returns the coerced dict on success, or a list of
    GraphQLError on failure — so `isinstance(..., dict)` IS the verdict."""
    operation = parse(query).definitions[0]
    return get_variable_values(_SCHEMA, operation.variable_definitions, variables)


def _assert_coerces(query: str, variables: dict) -> None:
    coerced = _coerce(query, variables)
    assert isinstance(coerced, dict), (
        "payload the operations layer emits was rejected by the pinned schema: "
        f"{[str(e) for e in coerced]}"
    )


def _coercion_errors(query: str, variables: dict) -> str:
    """Assert coercion FAILED and return the joined error text."""
    coerced = _coerce(query, variables)
    assert not isinstance(coerced, dict), f"expected coercion to fail, got {coerced!r}"
    return " ".join(str(e) for e in coerced)


_PID = "6803111739545"


@pytest.mark.parametrize(
    "emit",
    [
        pytest.param(lambda: _emit(ops.update_product_title, _PID, "T", "t-handle"), id="title"),
        pytest.param(lambda: _emit(ops.update_product_description, _PID, "<p>d</p>"), id="desc"),
        pytest.param(
            lambda: _emit(ops.update_product_seo, _PID, {"title": "S", "description": "D"}),
            id="seo",
        ),
        pytest.param(lambda: _emit(ops.update_product_tags, _PID, ["a", "b"]), id="tags"),
        pytest.param(lambda: _emit(ops.update_product_status, _PID, "ARCHIVED"), id="status"),
        pytest.param(
            lambda: _emit(
                webhook_ops.create_webhook, "ORDERS_CREATE", "https://example.com/hook", "JSON"
            ),
            id="create_webhook",
        ),
    ],
)
def test_emitted_payloads_coerce_against_pinned_schema(emit):
    """Every one of Story 9.18's six broken writes, end to end.

    Note these call the real `operations` functions rather than restating a
    payload literal — restating one would pin whatever the test author believed
    the code sends, which is precisely the drift being guarded against."""
    query, variables = emit()
    _assert_coerces(query, variables)


# ---- the discriminating negatives (step 8) --------------------------------
#
# Without these, a coercion leg that silently accepted everything would pass
# and prove nothing. Each one reproduces the exact break this story fixed.


def test_coercion_rejects_the_removed_input_key_on_product_update():
    """The `"input"` payload key against the fixed document: `$product` simply
    goes unsupplied, which is the failure the five product tools would hit."""
    joined = _coercion_errors(
        UPDATE_PRODUCT, {"input": {"id": "gid://shopify/Product/1", "title": "T"}}
    )
    assert "$product" in joined
    assert "ProductUpdateInput!" in joined


def test_coercion_rejects_callback_url_on_webhook_subscription_input():
    """`callbackUrl` was removed from `WebhookSubscriptionInput` (it is now
    `uri`). This is the break that document validation could not see: the
    document above validates clean with this very payload attached."""
    joined = _coercion_errors(
        CREATE_WEBHOOK,
        {
            "topic": "ORDERS_CREATE",
            "webhookSubscription": {
                "callbackUrl": "https://example.com/hook",
                "format": "JSON",
            },
        },
    )
    assert "callbackUrl" in joined
    assert "WebhookSubscriptionInput" in joined


def test_document_validation_alone_cannot_see_the_webhook_break():
    """Pins WHY leg (b) exists, so nobody deletes it as redundant.

    The broken payload from the test above is attached to a document that
    passes `validate()` without complaint. If this assertion ever flips, the
    validator has grown the ability to see into variable-supplied input objects
    and the coercion leg's rationale would need rewriting — but until then,
    dropping leg (b) would silently re-open this exact hole."""
    assert validate(_SCHEMA, parse(CREATE_WEBHOOK)) == []
