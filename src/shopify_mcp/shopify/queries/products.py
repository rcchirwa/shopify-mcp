"""GraphQL query/mutation strings for the ``products`` domain.

Selection sets that the by-id and by-handle reads previously duplicated are now
factored into shared fragments (``ProductCoreFields``, ``ProductFullFields``) so
the by-id / by-handle pairs reuse one definition — the fragment-dedup goal of
Story 10.23 / A5. Fragments reference the ``$first`` / ``$after`` variables,
which every operation that spreads them declares.
"""

# Shared selection set for the lightweight single-product reads (by id / handle).
PRODUCT_CORE_FIELDS = """
fragment ProductCoreFields on Product {
  id
  title
  handle
  status
  bodyHtml
  variants(first: $first, after: $after) {
    nodes { id title sku }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Shared selection set for the full single-product reads (by id / handle).
PRODUCT_FULL_FIELDS = """
fragment ProductFullFields on Product {
  id
  title
  handle
  status
  bodyHtml
  tags
  productType
  vendor
  seo { title description }
  category {
    id
    name
    fullName
  }
  options {
    id
    name
    optionValues { id name }
  }
  variants(first: $first, after: $after) {
    nodes { id title sku }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Story 10.72: the outer products connection now carries $after + pageInfo so
# client.paginate() can walk it, and a $query variable for the status filter.
# $query is bound as a GraphQL *variable*, never string-interpolated — the
# operations layer maps a validated status constant to a fixed search fragment.
#
# The nested variants(first: 50) is a separate, still-unpaginated truncation:
# a product with more than 50 variants shows a partial variant list here, and
# shows it SILENTLY. client.paginate() cannot walk a connection nested inside
# another connection — the same constraint documented above GET_ORDERS.
#
# GET_ORDERS answers that constraint by selecting pageInfo { hasNextPage } on
# the nested connection anyway, so the cap is at least detected and warned
# about (Story 10.34 / A3). This query deliberately does NOT follow that half
# of the precedent either: Story 10.72's scope guard put the nested variants
# connection out of scope, so both the pagination AND the detection are
# deferred here. Recorded as a residual in docs/tech-debt.md rather than left
# to read as an oversight.
GET_PRODUCTS = """
query GetProducts($first: Int!, $after: String, $query: String) {
  products(first: $first, after: $after, query: $query) {
    nodes {
      id
      title
      handle
      status
      variants(first: 50) {
        nodes { id title }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

GET_PRODUCT_BY_ID = (
    PRODUCT_CORE_FIELDS
    + """
query GetProductById($id: ID!, $first: Int = 50, $after: String) {
  product(id: $id) {
    ...ProductCoreFields
  }
}
"""
)

GET_PRODUCT_BY_HANDLE = (
    PRODUCT_CORE_FIELDS
    + """
query GetProductByHandle($handle: String!, $first: Int = 50, $after: String) {
  productByHandle(handle: $handle) {
    ...ProductCoreFields
  }
}
"""
)

UPDATE_PRODUCT = """
mutation UpdateProduct($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id title handle }
    userErrors { field message }
  }
}
"""

GET_PRODUCTS_BY_COLLECTION = """
query GetProductsByCollection($handle: String!, $first: Int!) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    products(first: $first) {
      nodes { id title handle status }
    }
  }
}
"""

GET_PRODUCTS_WITH_DESCRIPTIONS = """
query GetProductsWithDescriptions($first: Int!) {
  products(first: $first) {
    nodes {
      id
      title
      handle
      status
      bodyHtml
    }
  }
}
"""

GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS = """
query GetProductsByCollectionWithDescriptions($handle: String!, $first: Int!) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    products(first: $first) {
      nodes {
        id
        title
        handle
        status
        bodyHtml
      }
    }
  }
}
"""

GET_PRODUCT_FULL_BY_ID = (
    PRODUCT_FULL_FIELDS
    + """
query GetProductFullById($id: ID!, $first: Int = 50, $after: String) {
  product(id: $id) {
    ...ProductFullFields
  }
}
"""
)

GET_PRODUCT_FULL_BY_HANDLE = (
    PRODUCT_FULL_FIELDS
    + """
query GetProductFullByHandle($handle: String!, $first: Int = 50, $after: String) {
  productByHandle(handle: $handle) {
    ...ProductFullFields
  }
}
"""
)

# Shopify caps this connection at 250 per request. A product with more memberships
# would need pagination; emit an at-cap warning so operators don't silently miss
# collections — the whole point of this tool is completeness on the vault path.
GET_PRODUCT_COLLECTIONS = """
query GetProductCollections($id: ID!) {
  product(id: $id) {
    id
    title
    collections(first: 250) {
      nodes {
        id
        handle
        title
        ruleSet { appliedDisjunctively }
      }
      pageInfo { hasNextPage }
    }
  }
}
"""

GET_PRODUCT_SEO_BY_ID = """
query GetProductSeoById($id: ID!) {
  product(id: $id) {
    id
    title
    seo { title description }
  }
}
"""

UPDATE_PRODUCT_TAGS = """
mutation UpdateProductTags($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id tags }
    userErrors { field message }
  }
}
"""

UPDATE_PRODUCT_STATUS = """
mutation UpdateProductStatus($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id status }
    userErrors { field message }
  }
}
"""

GET_PRODUCT_VARIANTS_POLICY = """
query GetProductVariantsPolicy($id: ID!, $first: Int!, $after: String) {
  product(id: $id) {
    id
    title
    variants(first: $first, after: $after) {
      nodes { id title inventoryPolicy }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

UPDATE_PRODUCT_VARIANTS_POLICY = """
mutation UpdateProductVariantsPolicy($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    product { id }
    productVariants { id inventoryPolicy }
    userErrors { field message }
  }
}
"""
