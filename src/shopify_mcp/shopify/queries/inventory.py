"""GraphQL query/mutation strings for the ``inventory`` domain.

The bottom layer of the ``inventory`` migration (Story 10.28 / A5, following the
products pilot in Story 10.23 and catalog_hygiene in Story 10.25). Pure strings —
no imports from ``shopify.operations`` or ``tools``.

**Shared fragment applies.** The two reads — ``GET_PRODUCT_INVENTORY`` (per
variant) and ``GET_INVENTORY_ITEM`` (single item) — both select the 2024-07+
``quantities(names: ["available"]) { name quantity }`` set on ``InventoryLevel``
verbatim, so it is factored into the ``InventoryLevelQuantities`` fragment both
queries spread (Story 10.28 / A5, AC3). Each read still adds its own
``location`` selection (``id name`` for the product read, ``id`` for the item
read), which GraphQL keeps query-local. Centralizing only the version-sensitive
``quantities`` selection means the next Admin-API quantity-shape change is a
one-line edit instead of two.
"""

# Shared selection set for the "available" quantity on an InventoryLevel.
# 2024-07+ replaced InventoryLevel.available with
# `quantities(names: [...]) { name quantity }`. The `available` name is the
# direct equivalent of the old field.
INVENTORY_LEVEL_QUANTITIES = """
fragment InventoryLevelQuantities on InventoryLevel {
  quantities(names: ["available"]) { name quantity }
}
"""

GET_PRODUCT_INVENTORY = (
    INVENTORY_LEVEL_QUANTITIES
    + """
query GetProductInventory($id: ID!, $first: Int!, $after: String) {
  product(id: $id) {
    title
    variants(first: $first, after: $after) {
      nodes {
        id
        title
        sku
        inventoryItem {
          id
          tracked
          inventoryLevels(first: 10) {
            nodes {
              ...InventoryLevelQuantities
              location { id name }
            }
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
)

# Per-variant toggle for InventoryItem.tracked. When tracked=false, Shopify's
# storefront reports the variant as available regardless of inventoryPolicy or
# quantity — the vault-product skill needs tracked=true before DENY + 0 take
# effect at the theme layer. Shopify Admin API 2024-10 exposes inventoryItemUpdate
# per item; no bulk variant taking a list of (id, input) pairs is documented in
# this API version, so the caller issues one mutation per variant.
UPDATE_INVENTORY_ITEM_TRACKED = """
mutation UpdateInventoryItemTracked($id: ID!, $input: InventoryItemInput!) {
  inventoryItemUpdate(id: $id, input: $input) {
    inventoryItem { id tracked }
    userErrors { field message }
  }
}
"""

GET_INVENTORY_ITEM = (
    INVENTORY_LEVEL_QUANTITIES
    + """
query GetInventoryItem($id: ID!) {
  inventoryItem(id: $id) {
    inventoryLevels(first: 10) {
      nodes {
        ...InventoryLevelQuantities
        location { id }
      }
    }
  }
}
"""
)


SET_INVENTORY = """
mutation SetInventory($input: InventorySetOnHandQuantitiesInput!) {
  inventorySetOnHandQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt }
    userErrors { field message }
  }
}
"""
