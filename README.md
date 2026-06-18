# Shopify MCP Server

A custom Python [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that connects Claude directly to a Shopify store via the **Admin GraphQL API**. Built for the **All or Nothing Cypher** drop store under the **Global Streetwear Syndicate** parent brand.

Enables Claude to read products, check inventory, manage collections, handle discounts, and review orders — all through natural language chat in Claude Desktop.

---

## Tools exposed to Claude

### Products
| Tool | Description |
|------|-------------|
| `get_products` | List all products with id, title, handle, status, variants |
| `get_product` | Fetch a single product by id or handle |
| `update_product_title` | Update a product title (preview + confirm pattern) |
| `update_product_description` | Update a product's HTML description |
| `update_product_tags` | Update product tags — replace / append / remove modes (preview + confirm) |
| `update_product_status` | Transition product status — ACTIVE / DRAFT / ARCHIVED (preview + confirm) |
| `update_variant_inventory_policy` | Set variant inventoryPolicy — DENY / CONTINUE (preview + confirm; defaults to all variants) |
| `update_product_pricing` | Bulk update variant `price` / `compareAtPrice` via `productVariantsBulkUpdate`; resolves variant IDs from numeric / GID / SKU (preview + confirm) |
| `get_products_by_collection` | List all products in a collection by handle |
| `get_product_collections` | List every collection a product belongs to (manual + smart, with type label) |

### Media
| Tool | Description |
|------|-------------|
| `list_product_media` | List all media (images, videos, 3D) attached to a product with IDs, alt text, status, preview URLs (read-only) |
| `upload_product_image` | Upload an image from a public https:// URL and attach it to a product; optional `position` for featured placement (preview + confirm) |
| `reorder_product_media` | Change the display order of media on a product; 1-indexed for callers, polls the async job (preview + confirm) |
| `update_product_media` | Update alt text on an existing piece of product media (preview + confirm) |
| `delete_product_media` | Remove one or more media items from a product by media ID (preview + confirm) |

Requires `write_files` and `write_products` scopes. Local-file source paths are not accepted in v1 — URL only.

### Catalog hygiene
| Tool | Description |
|------|-------------|
| `update_product_category` | Set or change a product's Standard Product Taxonomy category; accepts a TaxonomyCategory GID or a free-text search string with `resolve_strategy` (exact / best-match / reject-ambiguous) (preview + confirm) |
| `update_product_vendor` | Set or clear a product's vendor / brand name; pass `None` to clear; trims whitespace and enforces ≤ 255 chars (preview + confirm) |
| `update_product_type` | Set or clear a product's legacy free-text `productType` field via `productUpdate`; accepts numeric ID / GID / handle; empty or whitespace input clears the field (Shopify treats `""` as cleared) (preview + confirm; head + JSON tail per the Epic 9 amendment) |
| `update_variant_image_binding` | Bind existing product media to one or more variants; resolves `variantId` (numeric / GID / SKU), rejects media GIDs not on the product, treats already-bound media as idempotent no-op, appends only the delta via `productVariantAppendMedia` (preview + confirm; head + JSON tail per the Epic 9 amendment) |
| `set_product_metafields` | Set or update up to 25 metafields on Products or ProductVariants in one `metafieldsSet` call; validates owner GID, namespace (rejects reserved `app--*`), and value shape per type (numeric regex, JSON parse for `json` / `list.*`); per-entry errors surface in head + `errorsByIndex` map; emits `remediation` block on Shopify `ACCESS_DENIED` (preview + confirm; head + JSON tail) |
| `delete_product_metafields` | Delete up to 25 metafields from Products or ProductVariants via `metafieldsDelete`; each entry addresses by either `metafieldId` GID or the `{ownerId, namespace, key}` triple (resolves to GID before the mutation); Shopify `NOT_FOUND` is treated as idempotent success-with-note so re-running is safe; per-entry errors surface in head + `errorsByIndex` map (preview + confirm; head + JSON tail) |
| `get_product_metafields` | Read all metafields on a Shopify product, optionally filtered by `namespace` (e.g. `google` for Google Shopping feed diagnostics) and/or `keys`; accepts numeric ID, Product GID, or handle; `include_variants=true` also returns per-variant metafields; cursor-paginates both connections; returns dual head + JSON tail with `{product, metafields[], variantMetafields, totalFound}` (read-only — no new OAuth scopes; covered by existing `read_products`) |
| `update_product_options` | Rename a product's variant option name (e.g., "Size") and/or its option values (e.g., "M-CRM" → "Medium") via `productOptionUpdate`; accepts numeric ID / GID / handle; validates option + option-value GIDs against the product before mutating; idempotent no-op when target state already matches; defaults `variant_strategy` to `LEAVE_AS_IS` (preview + confirm; head + JSON tail per the Epic 9 amendment) |

### Inventory
| Tool | Description |
|------|-------------|
| `get_inventory` | Get inventory levels for all variants of a product (single query) |
| `update_inventory` | Set quantity for a variant at a location (preview + confirm) |
| `update_variant_inventory_tracking` | Toggle `InventoryItem.tracked` on variants — required on POD products before DENY + 0 take effect at the storefront (preview + confirm; defaults to all variants) |

### Collections
| Tool | Description |
|------|-------------|
| `get_collection` | Get collection details by handle (works for both manual and smart collections) |
| `update_collection` | Update collection title or description (preview + confirm) |

### Discounts
| Tool | Description |
|------|-------------|
| `get_discount_codes` | List all discount codes / price rules |
| `create_discount_code` | Create a percentage-off code with usage limits (preview + confirm) |

### Orders
| Tool | Description |
|------|-------------|
| `get_orders` | List recent orders with line items and traffic source |
| `get_order` | Fetch a single order by id |

### Sales channel publications
| Tool | Description |
|------|-------------|
| `list_sales_channels` | List every sales channel (publication) on the store |
| `get_product_publications` | Show which channels a product is published to, and which it is not |
| `publish_product_to_channels` | Publish a product to one or more channels (idempotent, preview + confirm) |
| `unpublish_product_from_channels` | Unpublish from one or more channels (idempotent, preview + confirm) |
| `set_product_publications` | Declarative — diff current vs. desired channels, apply minimal publish/unpublish (preview + confirm) |

Requires `read_publications` and `write_publications` scopes. If the app was installed before these were added, reinstall it on the store.

---

## Safety design

- All **write operations** return a preview and require `confirm=True` before any data is changed
- **URL handles** are never modified unless `change_handle=True` is explicitly passed
- Every write operation is logged to `aon_mcp_log.txt` with timestamp, tool name, and change detail
- Product titles are validated against AON/Vanish naming conventions after every update

---

## Requirements

- Python 3.10+
- A Shopify store with a [Custom App](https://help.shopify.com/en/manual/apps/app-types/custom-apps) and Admin API access token
- [Claude Desktop](https://claude.ai/download)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/shopify-mcp.git
cd shopify-mcp
```

### 2. Create a virtual environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -e .
```

Installs the repo as an editable package along with all runtime
dependencies (`mcp`, `python-dotenv`, `requests`, `gql[requests]`),
declared in [pyproject.toml](pyproject.toml). `shopify_client`, `tools`,
`validators`, and `_testing` become importable from any working
directory, and a `shopify-mcp` console command lands in `.venv/bin/`.

For contributors running the test suite, install with the `dev` extra
to also pull in `pytest`, `coverage`, `ruff`, and `mypy`:

```bash
pip install -e .[dev]
```

Run the offline suite, lint, and type-check the same way CI does:

```bash
coverage run -m pytest test_*_offline.py -v
coverage report --fail-under=100
ruff check .
ruff format --check .   # or `ruff format .` to apply fixes
mypy
```

### 4. Configure credentials

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:
```
SHOPIFY_STORE_URL=your-store.myshopify.com
SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
SHOPIFY_API_VERSION=2026-01
```

### 5. Create the Shopify Custom App

In your Shopify Admin → **Settings → Apps and sales channels → Develop apps**:

1. Create a new app
2. Under **Configuration**, enable these Admin API access scopes:

```
read_products
write_products
read_inventory
write_inventory
read_orders
read_price_rules
write_price_rules
read_discounts
write_discounts
read_publications
write_publications
write_files
```

3. Install the app and copy the **Admin API access token** into your `.env`

### 6. Test the connection

```bash
python3 test_shopify_mcp.py
```

Expected output:
```
Testing Shopify API connection...
  Connected to: Your Store Name (your-store.myshopify.com)
  Plan: Basic
  Currency: USD

Connection test PASSED.

Fetching first 3 products...
  [123456789] Product Title — ACTIVE
  ...
Product fetch test PASSED (3 products returned).

All tests passed. MCP server is ready to register with Claude Desktop.
```

### 7. Register with Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "shopify-aon": {
      "command": "/Users/YOUR_USERNAME/shopify-mcp/.venv/bin/shopify-mcp",
      "env": {
        "SHOPIFY_STORE_URL": "your-store.myshopify.com",
        "SHOPIFY_ACCESS_TOKEN": "shpat_xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "SHOPIFY_API_VERSION": "2024-10"
      }
    }
  }
}
```

The `shopify-mcp` command is registered by `pip install -e .` via the
`[project.scripts]` entry in [pyproject.toml](pyproject.toml) — it's a
thin wrapper around `shopify_mcp:main`. The older form (`command` set
to the venv's Python with `args: ["shopify_mcp.py"]`) still works if
you have it wired up; the console script is just a cleaner one-path
alternative.

Replace `YOUR_USERNAME` with your macOS username (`whoami` in Terminal).

Restart Claude Desktop. The Shopify tools will appear in the tools panel.

---

## Example usage in Claude chat

```
Get all products in the smokescreen-apr-2026 collection and check
each title against the AON naming convention.
```

```
Update the title of product 12345678 to:
"All or Nothing | Smokescreen Tee – Washed Charcoal"
```

```
Show me inventory levels for product 12345678.
```

---

## Project structure

```
shopify-mcp/
├── shopify_mcp.py          # MCP server entry point
├── shopify_client.py       # Shopify GraphQL API client
├── shopify/                # Shopify domain layer (independent of the MCP surface)
│   ├── _ids.py             # GID encode/decode helpers (to_gid / from_gid)
│   ├── _client.py          # GraphQLClient Protocol the operations layer depends on
│   ├── queries/            # GraphQL strings grouped by resource, reusable via fragments
│   │   ├── products.py
│   │   ├── catalog_hygiene.py
│   │   ├── collections.py
│   │   └── discounts.py
│   └── operations/         # Typed business-logic wrappers, callable without the MCP server
│       ├── products.py
│       ├── catalog_hygiene.py
│       ├── collections.py
│       └── discounts.py
├── tools/
│   ├── _log.py             # Write operation logger
│   ├── _gid.py             # Re-exports shopify._ids (back-compat shim)
│   ├── products.py         # MCP-tool surface: coercion, preview/confirm, formatting
│   ├── inventory.py
│   ├── collections.py
│   ├── discounts.py
│   ├── orders.py
│   ├── publications.py
│   └── media.py
├── validators/
│   └── naming.py           # AON + Vanish title convention validator
├── pyproject.toml          # Package metadata, deps, console script, test/coverage config
├── test_shopify_mcp.py
├── .env.example
└── .gitignore
```

### Layering (`shopify/` extraction — Story 10.23 / A5)

The `shopify/` package separates Shopify domain logic from the MCP-tool surface,
one-way: `tools/` → `shopify.operations` → `shopify.queries`. `shopify/` never
imports from `tools/` (enforced by `test_shopify_layering_offline.py`), so
operations like `shopify.operations.products.update_product_title(client, ...)`
are callable from non-MCP entry points (CLI, scripts) without importing FastMCP.
GraphQL strings live in `shopify.queries.*` and reuse shared fragments (e.g.
`ProductCoreFields` across the by-id and by-handle product reads, or
`ProductVendorFields` / `ProductTypeFields` / `ProductOptionsFields` across the
catalog-hygiene by-id and by-handle pairs). The `products` (pilot),
`catalog_hygiene`, `collections`, and `discounts` domains are migrated; the
remaining domains still define their queries inline in `tools/*.py` and migrate
one per PR. (`collections` and `discounts` define no shared fragment —
`collections` has a single by-handle read with no by-id twin, and `discounts` has
no by-id/by-handle pair at all, so neither has a duplicated selection set.)

---

## API layer

This project uses the **Shopify Admin GraphQL API** (version `2026-01`).

Most tool modules define their own GraphQL query or mutation strings at the top of the file; the migrated `products`, `catalog_hygiene`, `collections`, and `discounts` domains instead keep them under `shopify/queries/` (see the layering note above). All queries are executed through a single `ShopifyClient.execute(query, variables)` method in `shopify_client.py`, which handles:

- Authentication via `X-Shopify-Access-Token` header
- GraphQL transport errors (HTTP 4xx/5xx)
- GraphQL body errors (`errors` array in response)
- Mutation `userErrors` are checked at each call site before writing or logging

Global IDs (GIDs) returned by the GraphQL API (e.g. `gid://shopify/Product/123`) are converted to plain numeric IDs for display using the `from_gid()` helper. User-supplied numeric IDs are converted back to GIDs using `to_gid()` before being passed to queries.

### Why GraphQL over REST?

- **Single queries** — `get_inventory` previously made one HTTP call per product variant (N+1). GraphQL fetches all variant inventory levels in a single nested query.
- **Unified collections** — the REST API had separate `/custom_collections` and `/smart_collections` endpoints requiring a two-call fallback. GraphQL has one `Collection` type that covers both.
- **Shopify direction** — Shopify is deprecating the Admin REST API in favour of GraphQL.

---

## Naming convention

Product titles are validated against these brand formats:

**AON:** `All or Nothing | [Drop Name] [Product Type] – [Variant]`
> Example: `All or Nothing | Smokescreen Tee – Washed Charcoal`

**Vanish:** `Vanish | [Collection] [Product Type] – [Detail]`
> Example: `Vanish | Fall into Fashion Oversized Tee – Iconic V Logo`

---

## Built with

- [Anthropic MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Shopify Admin GraphQL API](https://shopify.dev/docs/api/admin-graphql)
- [gql](https://github.com/graphql-python/gql) — Python GraphQL client
- [python-dotenv](https://github.com/theskumar/python-dotenv)

---

*Built as part of the AON E-commerce Analytics Sprint — Global Streetwear Syndicate*
