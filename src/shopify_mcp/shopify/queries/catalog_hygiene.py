"""GraphQL query/mutation strings for the ``catalog_hygiene`` domain.

The bottom layer of the ``catalog_hygiene`` migration (Story 10.25 / A5, the
follow-up to the products pilot in Story 10.23). Pure strings + the dynamic
query builders that emit GraphQL — no imports from ``shopify.operations`` or
``tools``.

Selection sets that the by-id and by-handle reads previously duplicated are now
factored into shared fragments (``ProductVendorFields``, ``ProductTypeFields``,
``ProductOptionsFields``) so each by-id / by-handle pair reuses one definition —
the fragment-dedup goal of Story 10.25 / A5 (AC3).
"""

from typing import Any

# Cap for the mutation-response media connection — used in the
# PRODUCT_VARIANT_APPEND_MEDIA f-string below and in the tool's truncation note
# (re-exported from tools/catalog_hygiene.py).
VARIANT_MEDIA_RESPONSE_CAP = 100

# ---------------------------------------------------------------------------
# GraphQL — Story 9.3 (update_product_pricing)
# ---------------------------------------------------------------------------

GET_PRODUCT_VARIANTS_FOR_PRICING = """
query GetProductVariantsForPricing($id: ID!) {
  product(id: $id) {
    id
    title
    variants(first: 250) {
      nodes { id sku price compareAtPrice }
    }
  }
}
"""

UPDATE_PRODUCT_VARIANTS_PRICING = """
mutation UpdateProductVariantsPricing(
  $productId: ID!,
  $variants: [ProductVariantsBulkInput!]!
) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    product { id }
    productVariants { id sku price compareAtPrice }
    userErrors { field message }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL — Story 9.2 (update_product_vendor)
# ---------------------------------------------------------------------------

# Vendor lookup is intentionally lighter than the products.py GET_PRODUCT_BY_ID
# query — only the fields needed for the preview text and the idempotency
# check. The by-id and by-handle reads share one selection set via the
# ``ProductVendorFields`` fragment (Story 10.25 / A5, AC3).
PRODUCT_VENDOR_FIELDS = """
fragment ProductVendorFields on Product {
  id
  title
  vendor
}
"""

GET_PRODUCT_VENDOR = (
    PRODUCT_VENDOR_FIELDS
    + """
query GetProductVendor($id: ID!) {
  product(id: $id) {
    ...ProductVendorFields
  }
}
"""
)

# Handle-form resolver: only fired when the caller passes a non-numeric,
# non-GID identifier. Returns the GID + the same vendor/title fields so the
# preview path doesn't need a second round-trip after resolution.
GET_PRODUCT_VENDOR_BY_HANDLE = (
    PRODUCT_VENDOR_FIELDS
    + """
query GetProductVendorByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    ...ProductVendorFields
  }
}
"""
)

# Per spec: ProductUpdateInput (not the older `input: ProductInput!` shape used
# in tools/products.py). Documented in shopify-aon-mcp-catalog-tools-spec.md
# §"Tool 2 — Underlying Shopify GraphQL".
UPDATE_PRODUCT_VENDOR = """
mutation productUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id vendor }
    userErrors { field message }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL — Story 9.4 (update_product_type)
# ---------------------------------------------------------------------------

# Narrow read for the productType field — mirror of the vendor read. The by-id
# and by-handle reads share one selection set via the ``ProductTypeFields``
# fragment.
PRODUCT_TYPE_FIELDS = """
fragment ProductTypeFields on Product {
  id
  title
  productType
}
"""

GET_PRODUCT_TYPE = (
    PRODUCT_TYPE_FIELDS
    + """
query GetProductType($id: ID!) {
  product(id: $id) {
    ...ProductTypeFields
  }
}
"""
)

# Handle-form resolver — only fired when product_id is non-numeric and non-GID.
GET_PRODUCT_TYPE_BY_HANDLE = (
    PRODUCT_TYPE_FIELDS
    + """
query GetProductTypeByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    ...ProductTypeFields
  }
}
"""
)

# ProductUpdateInput shape — same input variable as the vendor mutation. Per
# spec, clearing productType is wire-encoded as `productType: ""` (Shopify
# treats empty string as cleared for this field, unlike vendor where null is
# the clear path).
UPDATE_PRODUCT_TYPE = """
mutation productUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id productType }
    userErrors { field message }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL — Story 9.1 (update_product_category)
# ---------------------------------------------------------------------------

# Look up a Product when only its `handle` is known. Story 9.1 accepts numeric
# ID / GID / handle for `productId`; the first two short-circuit via to_gid,
# the handle path needs a query.
GET_PRODUCT_BY_HANDLE_MIN = """
query GetProductByHandleMin($handle: String!) {
  productByHandle(handle: $handle) {
    id
  }
}
"""

# Read current category for idempotency check + post-write snapshot. Narrower
# than tools.products.GET_PRODUCT_BY_ID — we only need id/title/category here.
GET_PRODUCT_CATEGORY = """
query GetProductCategory($id: ID!) {
  product(id: $id) {
    id
    title
    category { id fullName name }
  }
}
"""

# Shopify's Standard Product Taxonomy search. Returns up to 10 ranked nodes —
# rank is the order Shopify returns; there is no explicit `score` field.
TAXONOMY_SEARCH = """
query taxonomyCategories($search: String!) {
  taxonomy {
    categories(search: $search, first: 10) {
      nodes { id fullName name level isLeaf isRoot }
    }
  }
}
"""

# 2025+ Admin API takes `ProductUpdateInput` (singular `product:` argument);
# the legacy `ProductInput` shape is what `tools/products.py` still uses for
# title/handle/seo writes. Story 9.1 spec pins the new shape — keep them
# distinct so a future API-version bump doesn't have to untangle them.
UPDATE_PRODUCT_CATEGORY = """
mutation productUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product {
      id
      title
      category { id fullName name }
    }
    userErrors { field message }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL — Story 9.6 (update_variant_image_binding)
# ---------------------------------------------------------------------------

# Combined fetch: product media set (cross-product validation, AC #4) AND
# per-variant currently-bound media (idempotent detection, AC #6) in one
# round-trip. The SKU index also rides along — so if the caller mixes SKUs
# in, no extra resolver fetch is needed (the variants list is already here).
GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA = """
query GetProductMediaAndVariantMedia($id: ID!, $mediaFirst: Int!, $mediaAfter: String) {
  product(id: $id) {
    id
    title
    media(first: $mediaFirst, after: $mediaAfter) {
      nodes {
        id
        alt
        mediaContentType
        ... on MediaImage { image { url } }
      }
      pageInfo { hasNextPage endCursor }
    }
    variants(first: 250) {
      nodes {
        id
        sku
        media(first: 100) { nodes { id } }
      }
    }
  }
}
"""

# Page-2+ companion to GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA — fetches only media
# so variants are not re-fetched on every subsequent page.
GET_PRODUCT_MEDIA_PAGE = """
query GetProductMediaPage($id: ID!, $mediaFirst: Int!, $mediaAfter: String!) {
  product(id: $id) {
    id
    media(first: $mediaFirst, after: $mediaAfter) {
      nodes {
        id
        alt
        mediaContentType
        ... on MediaImage { image { url } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PRODUCT_VARIANT_APPEND_MEDIA = f"""
mutation ProductVariantAppendMedia(
  $productId: ID!,
  $variantMedia: [ProductVariantAppendMediaInput!]!
) {{
  productVariantAppendMedia(productId: $productId, variantMedia: $variantMedia) {{
    productVariants {{
      id
      media(first: {VARIANT_MEDIA_RESPONSE_CAP}) {{
        nodes {{
          id
          alt
          mediaContentType
          ... on MediaImage {{ image {{ url }} }}
        }}
        pageInfo {{ hasNextPage }}
      }}
    }}
    userErrors {{ field message }}
  }}
}}
"""

# Sequential complement to PRODUCT_VARIANT_APPEND_MEDIA. Shopify rejects
# productVariantAppendMedia for any variant that already has ANY media bound,
# so existing bindings must be cleared first. Unlike Append, Detach accepts
# multiple mediaIds per entry and has no pre-condition. NOT exposed as a
# standalone MCP tool — only the update_variant_image_binding composite flow
# calls this.
PRODUCT_VARIANT_DETACH_MEDIA = """
mutation ProductVariantDetachMedia(
  $productId: ID!,
  $variantMedia: [ProductVariantDetachMediaInput!]!
) {
  productVariantDetachMedia(productId: $productId, variantMedia: $variantMedia) {
    product { id }
    userErrors { field message }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL — Story 9.7 (set_product_metafields)
# ---------------------------------------------------------------------------

# `userErrors { code }` is REQUIRED here (vs. just `field message` in the
# other Epic 9 mutations) so the ACCESS_DENIED branch in AC #10 can detect
# the scope-block signal without falling back to message-string matching.
METAFIELDS_SET_MUTATION = """
mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields {
      id
      namespace
      key
      value
      type
      ownerType
    }
    userErrors { field message code }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL — Story 9.10 (delete_product_metafields)
# ---------------------------------------------------------------------------

# Resolution is batched via dynamic alias-based query construction (see
# `_build_batch_resolve_query`). One round-trip resolves the whole input
# batch — drops the resolve phase from O(N) round-trips to O(1) for any
# batch of N ≤ 25.

# `metafieldsDelete` returns the plain `UserError` type, which exposes only
# `field` and `message` — NOT `code` (unlike `metafieldsSet`, whose
# `MetafieldsSetUserError` does carry `code`). Selecting `code` here makes the
# whole query invalid ("Field 'code' doesn't exist on type 'UserError'"), so
# the mutation never executes (Story 9.11 live bug). The idempotent
# NOT_FOUND-as-success branch therefore reads the signal from the message text
# (see `_is_metafield_not_found_error` in tools/catalog_hygiene.py); the
# primary idempotency path is the pre-mutation resolve that skips absent
# metafields entirely.
METAFIELDS_DELETE_MUTATION = """
mutation metafieldsDelete($metafields: [MetafieldIdentifierInput!]!) {
  metafieldsDelete(metafields: $metafields) {
    deletedMetafields {
      ownerId
      namespace
      key
    }
    userErrors { field message }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL — Story 9.11 (get_product_metafields)
# ---------------------------------------------------------------------------

# Metafield read queries are built per-call from the helpers below. Shopify's
# Admin API rejects the simultaneous presence of `namespace` and `keys` on the
# `metafields(...)` connection — the *declaration* of both args in the query
# string triggers the rejection, not just non-null runtime values. So the
# query is emitted in one of three modes driven by the caller's filter input:
#   "keys"      → metafields(keys: $keys, ...)         (keys are fully qualified
#                                                       as "<namespace>.<key>"
#                                                       when both filters were
#                                                       supplied)
#   "namespace" → metafields(namespace: $namespace, ...)
#   "none"      → metafields(...)                       (no filter args)
#
# The same mode is reused for product-level and variant-level connections in
# the combined query — Shopify enforces the exclusivity rule independently
# on each connection.

_VALID_FILTER_MODES = ("keys", "namespace", "none")

_METAFIELD_NODE_FIELD_NAMES = (
    "id",
    "namespace",
    "key",
    "value",
    "type",
    "createdAt",
    "updatedAt",
)


def _metafield_node_fields(indent: int) -> str:
    """Selection-set fields for a Metafield node, each indented `indent` spaces.

    Used by the read-query builders so the same field list renders at the
    right depth for both the product-level and variant-level connections.
    """
    pad = " " * indent
    return "\n".join(pad + f for f in _METAFIELD_NODE_FIELD_NAMES)


def _metafield_filter_decls(mode: str) -> str:
    """GraphQL variable declarations for the chosen filter mode."""
    if mode == "keys":
        return "  $keys: [String!]\n"
    if mode == "namespace":
        return "  $namespace: String\n"
    return ""


def _metafield_filter_args(mode: str) -> str:
    """GraphQL argument fragment (with trailing comma+space) for the chosen mode."""
    if mode == "keys":
        return "keys: $keys, "
    if mode == "namespace":
        return "namespace: $namespace, "
    return ""


def _check_filter_mode(mode: str) -> None:
    """Defense in depth: each `_build_*` entry point validates `mode` so a
    future refactor that forgets to thread the closed-enum contract through
    fails loudly instead of silently emitting a no-filter query."""
    if mode not in _VALID_FILTER_MODES:
        raise ValueError(f"unknown metafield filter mode: {mode!r}")


def _build_get_product_metafields_query(mode: str) -> str:
    """Emit the product-only metafields read query for the chosen filter mode."""
    _check_filter_mode(mode)
    node_fields = _metafield_node_fields(10)
    return f"""
query GetProductMetafields(
  $id: ID!
{_metafield_filter_decls(mode)}  $first: Int!
  $after: String
) {{
  product(id: $id) {{
    id
    title
    handle
    metafields({_metafield_filter_args(mode)}first: $first, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      edges {{
        node {{
{node_fields}
        }}
      }}
    }}
  }}
}}
"""


def _build_get_product_and_variant_metafields_query(mode: str) -> str:
    """Emit the combined product + variants metafields read query.

    Both the product-level and variant-level `metafields(...)` connections
    use the same `mode` argument set.
    """
    _check_filter_mode(mode)
    product_node_fields = _metafield_node_fields(10)
    variant_node_fields = _metafield_node_fields(16)
    return f"""
query GetProductAndVariantMetafields(
  $id: ID!
{_metafield_filter_decls(mode)}  $first: Int!
  $after: String
  $variantsFirst: Int!
  $variantsAfter: String
) {{
  product(id: $id) {{
    id
    title
    handle
    metafields({_metafield_filter_args(mode)}first: $first, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      edges {{
        node {{
{product_node_fields}
        }}
      }}
    }}
    variants(first: $variantsFirst, after: $variantsAfter) {{
      pageInfo {{ hasNextPage endCursor }}
      edges {{
        node {{
          id
          title
          sku
          metafields({_metafield_filter_args(mode)}first: $first) {{
            edges {{
              node {{
{variant_node_fields}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def _build_get_product_variant_metafields_page_query(mode: str) -> str:
    """Emit the variants-only continuation page query.

    Used when the metafields connection is already exhausted but
    `include_variants=True` still needs more variant pages.
    """
    _check_filter_mode(mode)
    variant_node_fields = _metafield_node_fields(16)
    return f"""
query GetProductVariantMetafieldsPage(
  $id: ID!
{_metafield_filter_decls(mode)}  $first: Int!
  $variantsFirst: Int!
  $variantsAfter: String
) {{
  product(id: $id) {{
    id
    title
    handle
    variants(first: $variantsFirst, after: $variantsAfter) {{
      pageInfo {{ hasNextPage endCursor }}
      edges {{
        node {{
          id
          title
          sku
          metafields({_metafield_filter_args(mode)}first: $first) {{
            edges {{
              node {{
{variant_node_fields}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def _build_batch_resolve_query(
    classified: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Build a single GraphQL query that resolves every classified entry.

    `classified` is the per-entry intermediate produced by the tool's
    classification phase. Each entry's `mode` selects the resolution shape:
      - mode='gid'    : {idx, mode, gid}                    → look up the metafield
                        directly by Metafield GID; the response carries the
                        triple + ownerType + parent GID.
      - mode='triple' : {idx, mode, ownerId, ownerType,     → look up the metafield
                        namespace, key}                       via the owner's
                                                              `metafield(namespace, key)`
                                                              field. `ownerId` is
                                                              expected to be a
                                                              fully-resolved GID
                                                              (handles must be
                                                              pre-resolved by the
                                                              caller).

    Returns `(query_string, variables_dict)`. The response uses sequentially
    numbered aliases `e0`, `e1`, … (one per entry, in classified order). The
    per-alias shape under each mode:
      - mode='gid'    → `{id, namespace, key, ownerType, owner: {id}}`
                        OR `null` / `{}` when the metafield doesn't exist.
      - mode='triple' → `{metafield: {id, namespace, key, ownerType}}`
                        OR `{metafield: null}` (owner exists, metafield doesn't)
                        OR `null` (owner GID didn't resolve — hard error).

    Building the query dynamically keeps the resolve phase to one round-trip
    regardless of batch size; the alternative (separate static queries per
    mode + per-entry execution) was O(N) round-trips, which dominates the
    tool's latency for batched hygiene passes.
    """
    var_decls: list[str] = []
    selections: list[str] = []
    variables: dict[str, Any] = {}
    for i, c in enumerate(classified):
        if c["mode"] == "gid":
            var_decls.append(f"$id{i}: ID!")
            variables[f"id{i}"] = c["gid"]
            selections.append(
                f"  e{i}: node(id: $id{i}) {{\n"
                f"    ... on Metafield {{\n"
                f"      id namespace key ownerType\n"
                f"      owner {{ ... on Product {{ id }} ... on ProductVariant {{ id }} }}\n"
                f"    }}\n"
                f"  }}"
            )
        else:  # mode == "triple"
            var_decls.append(f"$ownerId{i}: ID!")
            var_decls.append(f"$ns{i}: String!")
            var_decls.append(f"$k{i}: String!")
            variables[f"ownerId{i}"] = c["ownerId"]
            variables[f"ns{i}"] = c["namespace"]
            variables[f"k{i}"] = c["key"]
            selections.append(
                f"  e{i}: node(id: $ownerId{i}) {{\n"
                f"    ... on Product {{ metafield(namespace: $ns{i}, key: $k{i}) "
                f"{{ id namespace key ownerType }} }}\n"
                f"    ... on ProductVariant {{ metafield(namespace: $ns{i}, key: $k{i}) "
                f"{{ id namespace key ownerType }} }}\n"
                f"  }}"
            )
    query = (
        f"query BatchResolveMetafields({', '.join(var_decls)}) {{\n" + "\n".join(selections) + "\n}"
    )
    return query, variables


# ---------------------------------------------------------------------------
# GraphQL — Story 9.5 (update_product_options)
# ---------------------------------------------------------------------------

# Narrow read: option + value GIDs for the tool-side child-of-option validation
# AND the post-write snapshot's variants slice. variants(first: 50) is kept as a
# fixed slice — the warn path (not full pagination) closes T-9.5-variants-cap.
# pageInfo{hasNextPage} is selected so update_product_options can emit an at-cap
# warning when a product has >50 variants. The by-id and by-handle reads share
# one selection set via the ``ProductOptionsFields`` fragment. The
# UPDATE_PRODUCT_OPTION echo below (variants(first: 50)) is an un-paginable
# mutation-response echo, a known remaining exception documented in the plan.
PRODUCT_OPTIONS_FIELDS = """
fragment ProductOptionsFields on Product {
  id
  title
  options {
    id
    name
    optionValues { id name }
  }
  variants(first: 50) {
    nodes { id title selectedOptions { name value } }
    pageInfo { hasNextPage }
  }
}
"""

GET_PRODUCT_OPTIONS = (
    PRODUCT_OPTIONS_FIELDS
    + """
query GetProductOptions($id: ID!) {
  product(id: $id) {
    ...ProductOptionsFields
  }
}
"""
)

# Handle-form resolver — fired only when product_id is non-numeric and non-GID.
# Returns the same shape as GET_PRODUCT_OPTIONS so callers don't branch on the
# read result.
GET_PRODUCT_OPTIONS_BY_HANDLE = (
    PRODUCT_OPTIONS_FIELDS
    + """
query GetProductOptionsByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    ...ProductOptionsFields
  }
}
"""
)

# `productOptionUpdate` updates exactly one option per call (Shopify limit).
# The tool signature only accepts a single `option` arg, so the multi-option
# case is impossible to express — see AC #10. `userErrors { code }` is required
# here so a DUPLICATE_OPTION_VALUE_NAME (rename collision) is surfacable as a
# structured error rather than message-string matching.
UPDATE_PRODUCT_OPTION = """
mutation productOptionUpdate(
  $productId: ID!,
  $option: OptionUpdateInput!,
  $optionValuesToUpdate: [OptionValueUpdateInput!],
  $variantStrategy: ProductOptionUpdateVariantStrategy
) {
  productOptionUpdate(
    productId: $productId,
    option: $option,
    optionValuesToUpdate: $optionValuesToUpdate,
    variantStrategy: $variantStrategy
  ) {
    product {
      id
      options { id name optionValues { id name } }
      variants(first: 50) { nodes { id title selectedOptions { name value } } }
    }
    userErrors { field message code }
  }
}
"""
