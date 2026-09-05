"""GraphQL query/mutation strings for the ``collections`` domain.

The bottom layer of the ``collections`` migration (Story 10.26 / A5, the
follow-up to the ``products`` pilot in Story 10.23 and ``catalog_hygiene`` in
Story 10.25). Pure strings — no imports from ``shopify.operations`` or
``tools``.

Unlike the ``products`` and ``catalog_hygiene`` domains, ``collections`` has a
single by-handle read (``GET_COLLECTION_BY_HANDLE``) and no by-id twin, so no
read-side selection set was duplicated. No fragment is extracted — fragment
dedup is opportunistic here (Story 10.26 / A5, AC3).

Story 10.82 changed the premise without changing the conclusion, so read this
before assuming the original rationale still holds: ``CREATE_COLLECTION`` and
``UPDATE_COLLECTION`` now carry byte-identical selection sets. A duplicated
selection set therefore DOES exist in this module. It is still not factored
out, on different grounds — the set is four scalars across two mutations, and a
fragment would add a named indirection that costs more to follow than the four
lines it saves. Revisit at a third identical mutation payload.
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

# Story 10.82 / T-collection-create. Same `CollectionInput` and same selection
# set as UPDATE_COLLECTION — `handle` is selected because Shopify may derive or
# suffix it, so the caller has to be told which handle it actually got.
#
# Manual collections only (decision 1, recorded on the card 2026-09-05): no
# `ruleSet` is ever placed in the input, and no `publications` either — a
# created collection is unpublished on every sales channel, which Story 10.83
# owns.
CREATE_COLLECTION = """
mutation CreateCollection($input: CollectionInput!) {
  collectionCreate(input: $input) {
    collection { id title handle }
    userErrors { field message }
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
