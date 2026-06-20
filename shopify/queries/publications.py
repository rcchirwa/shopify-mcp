"""GraphQL query/mutation strings for the ``publications`` domain.

The bottom layer of the ``publications`` migration (Story 10.30 / A5, following the
products pilot in Story 10.23 and the catalog_hygiene / collections / discounts /
inventory / orders migrations). Pure strings — no imports from
``shopify.operations`` or ``tools``.

**Shared fragment applies.** The two product reads — ``GET_PRODUCT_PUBLICATIONS_BY_ID``
and ``GET_PRODUCT_PUBLICATIONS_BY_HANDLE`` — differ only in their root field
(``product(id:)`` vs ``productByHandle(handle:)``); the entire ``Product`` selection
they wrap (``id title handle`` + the paginated ``resourcePublications`` connection)
is byte-identical, so it is factored into the ``ProductPublicationsFields`` fragment
both queries spread (Story 10.30 / A5, AC3 — the fragment-dedup win the card calls
out). The fragment references the operations' ``$first``/``$after`` pagination
variables, which both ``GetProductPublicationsById`` and
``GetProductPublicationsByHandle`` declare. The list read (``LIST_PUBLICATIONS``)
and the two mutations select different shapes, so the fragment is scoped to the pair.
"""

LIST_PUBLICATIONS = """
query ListPublications($first: Int!, $after: String) {
  publications(first: $first, after: $after) {
    nodes {
      id
      name
      supportsFuturePublishing
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Shared Product selection for the by-id/by-handle resourcePublications reads.
# `resourcePublications(first: $first, after: $after)` carries the pagination
# variables the two operations declare, so the fragment is usable only inside an
# operation that defines `$first`/`$after` — both reads below do.
PRODUCT_PUBLICATIONS_FIELDS = """
fragment ProductPublicationsFields on Product {
  id
  title
  handle
  resourcePublications(first: $first, after: $after) {
    nodes {
      publication { id name }
      publishDate
      isPublished
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

GET_PRODUCT_PUBLICATIONS_BY_ID = (
    PRODUCT_PUBLICATIONS_FIELDS
    + """
query GetProductPublicationsById($id: ID!, $first: Int!, $after: String) {
  product(id: $id) {
    ...ProductPublicationsFields
  }
}
"""
)

GET_PRODUCT_PUBLICATIONS_BY_HANDLE = (
    PRODUCT_PUBLICATIONS_FIELDS
    + """
query GetProductPublicationsByHandle($handle: String!, $first: Int!, $after: String) {
  productByHandle(handle: $handle) {
    ...ProductPublicationsFields
  }
}
"""
)

PUBLISHABLE_PUBLISH = """
mutation PublishableProductPublish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    publishable { ... on Product { id title } }
    userErrors { field message }
  }
}
"""

PUBLISHABLE_UNPUBLISH = """
mutation PublishableProductUnpublish($id: ID!, $input: [PublicationInput!]!) {
  publishableUnpublish(id: $id, input: $input) {
    publishable { ... on Product { id title } }
    userErrors { field message }
  }
}
"""
