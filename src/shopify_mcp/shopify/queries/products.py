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
# The nested variants connection is a separate truncation, and it stays
# unpaginated: client.paginate() walks a single cursor over a single connection
# and cannot follow a connection nested inside another one — the same constraint
# documented above GET_ORDERS. Even a $variantsAfter would not help, because one
# cursor cannot address "product N's variants" when every product on the page
# has its own. The remedy for a full variant list is get_product, which walks a
# single product's variants.
#
# Story 10.77 closes the half that WAS achievable, following GET_ORDERS the rest
# of the way (Story 10.34 / A3): pageInfo { hasNextPage } is selected on the
# nested connection, so the cap is DETECTED rather than silent, and get_products
# emits a per-product at-cap WARNING. The cap itself is bound as $variantsFirst
# from GET_PRODUCTS_VARIANT_CAP in shopify.operations.products rather than
# written as a literal here, so the number in this query and the number in that
# warning cannot drift apart.
GET_PRODUCTS = """
query GetProducts($first: Int!, $after: String, $query: String, $variantsFirst: Int!) {
  products(first: $first, after: $after, query: $query) {
    nodes {
      id
      title
      handle
      status
      variants(first: $variantsFirst) {
        nodes { id title }
        pageInfo { hasNextPage }
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

# Story 10.76: the three queries below feed client.paginate(), which requires
# $first + $after and a pageInfo { hasNextPage endCursor } selection on the
# connection it walks. The two collection-scoped ones nest that connection
# inside collectionByHandle, so their connection_path is
# ["collectionByHandle", "products"] and the collection's own id/title/handle
# come off paginate()'s first-page response rather than off the node list.
GET_PRODUCTS_BY_COLLECTION = """
query GetProductsByCollection($handle: String!, $first: Int!, $after: String) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    products(first: $first, after: $after) {
      nodes { id title handle status }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

GET_PRODUCTS_WITH_DESCRIPTIONS = """
query GetProductsWithDescriptions($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    nodes {
      id
      title
      handle
      status
      bodyHtml
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

GET_PRODUCTS_BY_COLLECTION_WITH_DESCRIPTIONS = """
query GetProductsByCollectionWithDescriptions($handle: String!, $first: Int!, $after: String) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    products(first: $first, after: $after) {
      nodes {
        id
        title
        handle
        status
        bodyHtml
      }
      pageInfo { hasNextPage endCursor }
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
