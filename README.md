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
- `register_webhook` fails closed (SEC-17 / Story 10.51): it refuses to register an endpoint unless its hostname is listed in `WEBHOOK_ALLOWLIST_HOSTS`, and only ever accepts `https://` endpoints. See [Webhook endpoint allowlist](#webhook-endpoint-allowlist) below.

---

## Requirements

- Python 3.11+
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

Install from the lockfile — this is what CI does, and it gets you the exact
pinned + hash-verified versions instead of whatever `pyproject.toml`'s
`>=floor,<cap` ranges happen to resolve to on the day you install:

```bash
pip install --require-hashes -r requirements-dev.lock   # pytest, coverage, ruff, mypy + runtime deps
pip install --no-deps -e .                               # the package itself; deps already pinned above
```

`--require-hashes` makes pip refuse any package not listed in the lockfile
with a matching hash. The editable install puts a single top-level name —
`shopify_mcp` — on the import path, so the package is importable from any
working directory as `shopify_mcp.tools`, `shopify_mcp.client` and so on
(Story 10.47; the previous layout installed seven generic names instead).
A `shopify-mcp` console command lands in `.venv/bin/`.

Runtime-only installs (no dev tooling) can use `requirements.lock` instead of
`requirements-dev.lock`.

Run the offline suite, lint, and type-check the same way CI does. `pytest`
takes no test paths: `pyproject.toml` scopes discovery to `tests/`, and
`tests/conftest.py` holds back `tests/live/` — the live smoke runners, which
need real store credentials. Run those deliberately with `pytest tests/live`:

```bash
coverage run -m pytest -v
coverage report --fail-under=100
ruff check .
ruff format --check .   # or `ruff format .` to apply fixes
mypy
```

#### Keeping your environment in sync

When new dependencies land in [pyproject.toml](pyproject.toml) (e.g. a PR adds
a runtime dep or a type stub), an environment you created earlier will be
**stale** until you reinstall — the symptom is a `ModuleNotFoundError` at server
boot, or mypy reporting `import-untyped` for a package whose stubs you never
pulled. After every `git pull`, re-sync with:

```bash
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps -e .
```

To check whether the current interpreter has drifted out of sync without a full
reinstall, run the dep-drift check — it compares every dependency declared in
pyproject.toml against what is actually installed and exits non-zero (listing
what is missing) if they diverge:

```bash
shopify-mcp-check-deps        # or: python -m shopify_mcp.depcheck
```

It checks that each declared distribution is *installed* (presence, not version
bounds — pip enforces those at install time). That's exactly what catches the
boot crash: because Claude Desktop is pinned to `.venv/bin/shopify-mcp` (see
step 7), pointing that same `.venv` at the check confirms the server won't hit a
missing-dependency `ModuleNotFoundError` at startup. The same check runs in CI
so a declared-but-unresolvable dependency can't pass review. depcheck stays a
**presence-only** complement to the lockfile + CVE scan below — it doesn't
check pinned versions or vulnerabilities, `requirements*.lock` and `pip-audit`
do.

#### Regenerating the lockfile (Story 10.40 / SEC-13, SEC-14)

`requirements.lock` (runtime deps) and `requirements-dev.lock` (runtime + the
`dev` extra) are generated from `pyproject.toml` by
[`pip-tools`](https://github.com/jazzband/pip-tools). Whenever a dependency in
`pyproject.toml` changes — added, removed, or its version range edited —
regenerate both:

```bash
pip install pip-tools
pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements.lock pyproject.toml
pip-compile --extra dev --generate-hashes --allow-unsafe --strip-extras -o requirements-dev.lock pyproject.toml
```

Commit the regenerated lockfiles alongside the `pyproject.toml` change. CI's
`dependency-audit` job (`.github/workflows/test.yml`) runs
[`pip-audit`](https://github.com/pypa/pip-audit) against **both**
`requirements.lock` and `requirements-dev.lock` on every PR and fails the
build if any pinned dependency in either file has a known CVE — if it fails,
bump the affected package's floor past the fixed version in `pyproject.toml`
and regenerate the lockfiles, the same way `pytest` was bumped `>=7,<9` →
`>=9.0.3,<10` to close CVE-2025-71176.

`pip-audit` itself is pinned in a third lockfile, `requirements-audit.lock`,
generated from `requirements-audit.in` — the CI job installs it with
`--require-hashes` too, so the scanner isn't a floating, unverified install in
the job that exists to catch exactly that. Regenerate it the same way when its
version range changes:

```bash
pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements-audit.lock requirements-audit.in
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

#### Webhook endpoint allowlist

`register_webhook` fails closed by default (SEC-17 / Story 10.51): with
`WEBHOOK_ALLOWLIST_HOSTS` unset, every call is refused outright — no webhook
is created, even with `confirm=True`. To use `register_webhook`, set the
comma-separated list of hostnames you intend to register as endpoints:

```
WEBHOOK_ALLOWLIST_HOSTS=your-app.railway.app
```

Endpoints must also use `https://`; any other scheme is refused before any
hostname or network work happens, including for an allowlisted host.

If you need to register endpoints outside a known allowlist (e.g. local
experimentation), you can opt back into the pre-SEC-17 warn-and-proceed
behavior — any `https://` host is allowed, with a `⚠ EXTERNAL DOMAIN`
warning shown in the preview — by setting:

```
WEBHOOK_ALLOW_ANY_HOST=true
```

This is an explicit escape hatch, not the default posture — prefer a real
allowlist entry over leaving this set in a shared or production `.env`.

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
python3 tests/live/test_server.py
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
thin wrapper around `shopify_mcp.server:main`. If you have the older form
wired up (`command` set to the venv's Python with `args: ["shopify_mcp.py"]`),
**it no longer works**: Story 10.47 moved the entry point to
`src/shopify_mcp/server.py`, so there is no `shopify_mcp.py` at the repo root
to run. Use the console script above, or the module form
`args: ["-m", "shopify_mcp"]`.

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

Everything the project ships lives under one top-level name, `shopify_mcp`, in
a `src/` layout (Story 10.47). Two consequences worth knowing:

- **The working tree is not importable.** `src/` is not on `sys.path`, so
  `pytest` exercises the *installed* distribution rather than the checkout —
  which is why the editable install (`pip install --no-deps -e .`, step 3) is
  required before the suite will run, and why a module accidentally left out
  of the package can no longer pass CI and break only on a real install.
- **No generic top-level names.** The previous layout installed `tools`,
  `settings`, `validators`, `shopify` and three more onto the import path of
  any environment installing this project. `shopify` in particular is owned by
  the [ShopifyAPI](https://pypi.org/project/ShopifyAPI/) distribution, so the
  two silently contended for one name; the domain layer is now reachable only
  as `shopify_mcp.shopify`.

```
shopify-mcp/
├── src/
│   └── shopify_mcp/            # the one top-level name this project installs
│       ├── __main__.py         # `python -m shopify_mcp`
│       ├── server.py           # MCP server entry point (console script: shopify-mcp)
│       ├── client.py           # Shopify GraphQL API client
│       ├── settings.py         # Env-backed config (pydantic-settings)
│       ├── logging_config.py   # JSON logging setup
│       ├── depcheck.py         # Dependency-drift check (shopify-mcp-check-deps)
│       ├── shopify/            # Shopify domain layer (independent of the MCP surface)
│       │   ├── _ids.py         # GID encode/decode helpers (to_gid / from_gid)
│       │   ├── _client.py      # GraphQLClient Protocol the operations layer depends on
│       │   ├── queries/        # GraphQL strings grouped by resource, reusable via fragments
│       │   │   ├── products.py
│       │   │   ├── catalog_hygiene.py
│       │   │   ├── collections.py
│       │   │   ├── discounts.py
│       │   │   ├── inventory.py
│       │   │   ├── orders.py
│       │   │   ├── publications.py
│       │   │   └── webhooks.py
│       │   └── operations/     # Typed business-logic wrappers, callable without the MCP server
│       │       ├── products.py
│       │       ├── catalog_hygiene.py
│       │       ├── collections.py
│       │       ├── discounts.py
│       │       ├── inventory.py
│       │       ├── orders.py
│       │       ├── publications.py
│       │       └── webhooks.py
│       ├── tools/
│       │   ├── _log.py         # Write operation logger (audit log at the repo root)
│       │   ├── _gid.py         # Re-exports shopify._ids (back-compat shim)
│       │   ├── products.py     # MCP-tool surface: coercion, preview/confirm, formatting
│       │   ├── inventory.py
│       │   ├── collections.py
│       │   ├── discounts.py
│       │   ├── orders.py
│       │   ├── publications.py
│       │   └── media/
│       └── validators/
│           └── naming.py       # AON + Vanish title convention validator
├── tests/                      # Whole suite; mirrors the source layout (Story 10.45)
│   ├── conftest.py             # Session-wide fixtures
│   ├── support/                # Test doubles (FakeClient, CapturingServer) — not shipped (Story 10.46)
│   ├── unit/                   # One package per source package
│   │   ├── tools/              # 19 modules covering the MCP-tool surface
│   │   ├── shopify/            # test_cache.py + operations/ (8 modules)
│   │   ├── validators/         # test_naming.py
│   │   └── test_{client,depcheck,logging_config,settings,paginate}.py
│   ├── architecture/           # Structural guards: layering, lockfiles, docs, layout, packaging, src-layout
│   └── live/                   # Smoke runners needing real credentials — excluded by default
├── docs/                       # All non-README markdown lives here (Story 10.48)
│   ├── tech-debt.md            # Tactical / code-level debt ledger, triaged journal-style
│   ├── architecture-tech-debt.md  # Strategic / design-level debt ledger, static
│   └── specs/                  # Per-story implementation specs
├── pyproject.toml              # Package metadata, deps, console script, test/coverage config
├── aon_mcp_log.txt             # Write-audit trail (repo root, not inside src/)
├── .env.example
└── .gitignore
```

Every directory under `tests/` carries an `__init__.py`. That is load-bearing:
mirroring the source layout gives nine duplicate test basenames (e.g.
`unit/tools/test_products.py` vs `unit/shopify/operations/test_products.py`),
and under pytest's default *prepend* import mode duplicate basenames in
non-package directories abort collection outright. `tests/architecture/test_layout.py`
guards the invariant.

### Layering (`shopify/` extraction — Story 10.23 / A5)

The `shopify/` package separates Shopify domain logic from the MCP-tool surface,
one-way: `tools/` → `shopify.operations` → `shopify.queries`. `shopify/` never
imports from `tools/` (enforced by `tests/architecture/test_layering.py`), so
operations like `shopify.operations.products.update_product_title(client, ...)`
are callable from non-MCP entry points (CLI, scripts) without importing FastMCP.
GraphQL strings live in `shopify.queries.*` and reuse shared fragments (e.g.
`ProductCoreFields` across the by-id and by-handle product reads,
`ProductVendorFields` / `ProductTypeFields` / `ProductOptionsFields` across the
catalog-hygiene by-id and by-handle pairs, `InventoryLevelQuantities` across the
two inventory reads, `OrderCoreFields` across the two orders reads, or
`ProductPublicationsFields` across the two publications reads). All eight domains —
`products` (pilot), `catalog_hygiene`, `collections`, `discounts`, `inventory`,
`orders`, `publications`, and `webhooks` — are now migrated, **closing A5**.
(`collections`, `discounts`, and `webhooks` define no shared fragment —
`collections` has a single by-handle read with no by-id twin, `discounts` has no
by-id/by-handle pair, and `webhooks` is one list read plus two mutations with no
entity core shared across a read pair, so none has a duplicated selection set worth
factoring.)

---

## API layer

This project uses the **Shopify Admin GraphQL API** (version `2026-01`).

Some tool modules define their own GraphQL query or mutation strings at the top of the file; the migrated `products`, `catalog_hygiene`, `collections`, `discounts`, `inventory`, `orders`, `publications`, and `webhooks` domains instead keep them under `shopify/queries/` (see the layering note above). All queries are executed through a single `ShopifyClient.execute(query, variables)` method in `src/shopify_mcp/client.py`, which handles:

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
