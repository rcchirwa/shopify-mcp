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

# Story 10.83 (T-collection-publish). Collections has only a by-handle read —
# `_resolve_collection` in tools/collections.py is handle-only and no by-id
# collection query exists anywhere in the tree — so unlike the product pair
# there is no by-id twin to share a fragment with. The selection is inlined
# rather than factored, matching the reasoning in queries/collections.py.
#
# The selection is byte-identical in shape to ProductPublicationsFields, which
# is not an assumption: the 2026-09-05 live probe read a manual and a smart
# collection and both returned `publication { id name }`, `publishDate` and
# `isPublished` with the same pageInfo.
#
# `ruleSet` is selected so a caller can tell smart from manual in the output.
# It drives no branch — the probe found no read-side difference between them.
GET_COLLECTION_PUBLICATIONS_BY_HANDLE = """
query GetCollectionPublicationsByHandle($handle: String!, $first: Int!, $after: String) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    ruleSet { appliedDisjunctively }
    resourcePublications(first: $first, after: $after) {
      nodes {
        publication { id name }
        publishDate
        isPublished
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

# `publishablePublish` / `publishableUnpublish` are generic over Shopify's
# `Publishable` interface, so the same two mutations serve products and
# collections. Story 10.83 added the Collection inline fragment beside the
# Product one — without it a Collection target came back as an empty
# `publishable {}` — and dropped `Product` from the operation names, which
# stopped being accurate once a Collection could be the target. Those are
# GraphQL operation names inside a string, not part of any Python contract.
PUBLISHABLE_PUBLISH = """
mutation PublishablePublish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    publishable {
      ... on Product { id title }
      ... on Collection { id title }
    }
    userErrors { field message }
  }
}
"""

PUBLISHABLE_UNPUBLISH = """
mutation PublishableUnpublish($id: ID!, $input: [PublicationInput!]!) {
  publishableUnpublish(id: $id, input: $input) {
    publishable {
      ... on Product { id title }
      ... on Collection { id title }
    }
    userErrors { field message }
  }
}
"""
