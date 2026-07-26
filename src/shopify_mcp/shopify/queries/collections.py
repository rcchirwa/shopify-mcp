"""GraphQL query/mutation strings for the ``collections`` domain.

The bottom layer of the ``collections`` migration (Story 10.26 / A5, the
follow-up to the ``products`` pilot in Story 10.23 and ``catalog_hygiene`` in
Story 10.25). Pure strings — no imports from ``shopify.operations`` or
``tools``.

Unlike the ``products`` and ``catalog_hygiene`` domains, ``collections`` has a
single by-handle read (``GET_COLLECTION_BY_HANDLE``) and no by-id twin, so there
is no duplicated selection set to factor into a shared fragment. None is
extracted — fragment dedup is opportunistic here and does not apply (Story
10.26 / A5, AC3).
"""

GET_COLLECTION_BY_HANDLE = """
query GetCollectionByHandle($handle: String!) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    descriptionHtml
    ruleSet { appliedDisjunctively }
  }
}
"""

UPDATE_COLLECTION = """
mutation UpdateCollection($input: CollectionInput!) {
  collectionUpdate(input: $input) {
    collection { id title handle }
    userErrors { field message }
  }
}
"""

# Both membership mutations return an async `job` in 2024-07+. If the initial
# response has done=false, poll_job() blocks up to settings.job_poll_timeout_s
# so the caller sees a final done state instead of an indeterminate one.
ADD_PRODUCTS_TO_COLLECTION = """
mutation AddProductsToCollection($id: ID!, $productIds: [ID!]!) {
  collectionAddProductsV2(id: $id, productIds: $productIds) {
    job { id done }
    userErrors { field message }
  }
}
"""

REMOVE_PRODUCTS_FROM_COLLECTION = """
mutation RemoveProductsFromCollection($id: ID!, $productIds: [ID!]!) {
  collectionRemoveProducts(id: $id, productIds: $productIds) {
    job { id done }
    userErrors { field message }
  }
}
"""
