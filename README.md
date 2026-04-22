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
| `get_products_by_collection` | List all products in a collection by handle |

### Inventory
| Tool | Description |
|------|-------------|
| `get_inventory` | Get inventory levels for all variants of a product (single query) |
| `update_inventory` | Set quantity for a variant at a location (preview + confirm) |

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
pip install -r requirements.txt
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
SHOPIFY_API_VERSION=2024-01
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
      "command": "/Users/YOUR_USERNAME/shopify-mcp/.venv/bin/python",
      "args": ["/Users/YOUR_USERNAME/shopify-mcp/shopify_mcp.py"],
      "env": {
        "SHOPIFY_STORE_URL": "your-store.myshopify.com",
        "SHOPIFY_ACCESS_TOKEN": "shpat_xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "SHOPIFY_API_VERSION": "2024-01"
      }
    }
  }
}
```

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
├── tools/
│   ├── _log.py             # Write operation logger
│   ├── products.py
│   ├── inventory.py
│   ├── collections.py
│   ├── discounts.py
│   ├── orders.py
│   └── publications.py
├── validators/
│   └── naming.py           # AON + Vanish title convention validator
├── requirements.txt
├── test_shopify_mcp.py
├── .env.example
└── .gitignore
```

---

## API layer

This project uses the **Shopify Admin GraphQL API** (version `2024-01`).

Each tool module defines its own GraphQL query or mutation strings at the top of the file. All queries are executed through a single `ShopifyClient.execute(query, variables)` method in `shopify_client.py`, which handles:

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
