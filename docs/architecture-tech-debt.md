# Architectural Tech Debt

Strategic, design-level technical debt for `shopify-mcp`. Sibling to [tech-debt.md](tech-debt.md), which tracks tactical / code-level items.

**Scope:** patterns and structures that constrain future scaling, maintainability, or extensibility — debt that won't surface as a single failing test but will compound as the codebase grows.

**Format:** static ledger, updated when the architecture genuinely shifts (not after every PR). The journal-style triage workflow lives in tech-debt.md; this document captures the long-arc design concerns that don't move on a daily cadence.

**Scoring:** `Priority = (Impact + Risk) × (6 − Effort)`, each axis 1–5, effort inverted. Same framework as tech-debt.md so items can be triaged together when needed. **Ties broken by Impact descending, then by ID ascending.**

**Source:** initial inventory derived from the 2026-04-25 architecture review (10 evaluation areas across organization, tool surface, error handling, auth, caching, rate limiting, reuse, deps, config, observability). Items **A11–A14** were added by a 2026-07-25 folder-structure audit covering repository layout, test placement, and packaging — an axis the original review did not evaluate.

---

## Backlog (priority-ordered)

| Rank | ID | Item | Category | I | R | E | Score |
|------|----|------|----------|---|---|---|-------|
| 1 | A3 | Pagination helper for list reads — *helper shipped + read-path adoption Story 10.16* | Code | 2 | 3 | 3 | **15** |
| 2 | A2 | `write_gate()` helper collapsing preview/confirm/error/audit boilerplate — *closed Story 10.22; 9 tools migrated; remaining tools triaged and deliberately excluded* | Code | 4 | 2 | 4 | **12** |
| 3 | A5 | `shopify/` subpackage extraction (`queries/` + `operations/`) with GraphQL fragments — *closed Story 10.31; all 8 domains migrated (`products`, `catalog_hygiene`, `collections`, `discounts`, `inventory`, `orders`, `publications`, `webhooks`)* | Architecture | 2 | 1 | 2 | **12** |
| 4 | A6 | HTTP client unification (single wrapper for `gql` + `requests`) — *closed: policy half N4/Story 10.21, transport half Story 10.24 (`client.fetch_bytes()` + shared `_with_retry`)* | Architecture | 2 | 2 | 3 | **12** |
| 5 | A10 | Committed `uv.lock` for CI reproducibility | Dependency | 1 | 1 | 5 | **2** |

**Category coverage:** the 2026-04-25 review left Test debt and Documentation debt unrepresented, noting that coverage was at 100% and that tech-debt.md plus README covered most documentation needs. The 2026-07-25 folder-structure audit filled both gaps — **A11** (Test) and **A14** (Documentation). Note that neither was about *coverage* or *content*, which remain healthy; both were about *where files live*, which the original review's ten evaluation areas didn't probe.

**All four folder-structure items (A11–A14) are closed** — Stories 10.45–10.48, PRs [#114](https://github.com/rcchirwa/shopify-mcp/pull/114), [#116](https://github.com/rcchirwa/shopify-mcp/pull/116), [#117](https://github.com/rcchirwa/shopify-mcp/pull/117), [#118](https://github.com/rcchirwa/shopify-mcp/pull/118). Details in the [Closed](#closed) section; the backlog above is back to its pre-audit contents.

---

## Items

### A2 — `write_gate()` helper

- **Category:** Code
- **Status:** ✅ **closed — Story 10.22.** All standard-pattern tools have been migrated or deliberately excluded with technical justification. 9 tools now use `write_gate()`.
- **Cross-reference:** tech-debt.md item **#7** (closed by [#33](https://github.com/rcchirwa/shopify-mcp/pull/33)) collapsed only the hint-string duplication via `with_confirm_hint`. A2 wraps the wider write-tool flow (execute → error check → `log_write`) that #7 left untouched.
- **Impact (4):** every write tool benefits; future tools become 10–20 lines instead of 60–100.
- **Risk (2):** boilerplate duplication is the largest source of subtle drift between tools. A typo silently skips `log_write` or the confirm gate. The helper makes that omission structurally impossible in tools that adopt it.
- **Effort (4):** reduced from 3 — helper is shipped; remaining is mechanical per-tool migration with test verification.
- **Design choice:** helper function, not a decorator. A `@write_tool` decorator that fully owns the flow would require tool bodies to return structured data (preview + execute callable + log description) rather than `str` — that's a bigger API change. The `write_gate()` helper called at the return site achieves the same drift-prevention with zero framework magic; the name at the call site is self-documenting.
- **Phase 1 — initial helper + 7 tools (merged PR #87):**
  - `tools/_write_tool.py` — `write_gate()` helper centralising confirm gate, `format_user_errors` check, `log_write`, and done-string return. Accepts `done_text: str | None` for tools whose done string differs from preview, and `log_description: str | Callable[[], str]` so non-trivial descriptions aren't computed on the preview path.
  - `test_write_tool_offline.py` — 8 tests covering preview path, default done text, custom `done_text`, callable `log_description` (preview vs confirm, suppression on userErrors), userErrors short-circuit, custom `error_key`, `TransientShopifyError` propagation.
  - `conftest.py` — session-wide autouse fixture patching `_wt.log_write` so migrated tools don't pollute the audit log during tests.
  - Migrated tools: `products.update_product_title`, `update_product_description`, `update_product_seo`, `update_product_tags`, `update_product_status`; `collections.update_collection`; `inventory.update_inventory`.
- **Phase 2 — write_gate() extended + 2 webhook tools (Story 10.22):**
  - `tools/_write_tool.py` extended with two new parameters:
    - `done_text: str | Callable[[], str] | None` — callable variant lets the done string capture mutation result data (e.g. a newly-created subscription ID) via closure without polluting the preview path.
    - `post_execute_check: Callable[[dict], str | None] | None` — post-mutation validation hook called after userErrors pass, before `log_write`. A non-None return short-circuits success so `log_write` is never called on a structurally bad response.
  - `test_write_tool_offline.py` — extended to 15 tests (+7 covering the new parameters).
  - Migrated tools: `webhooks.register_webhook` (uses `captured{}` closure + `done_text` callable to surface subscription ID); `webhooks.delete_webhook` (uses `post_execute_check` to validate `deletedWebhookSubscriptionId` presence).
- **Deliberately excluded — standard-pattern tools with incompatible control flow:**
  - `publications.{publish,unpublish,set}_product_publications` — partial-success semantics: `log_write` is called even when some channels fail (partial success is normal for Shopify multi-channel mutations). Field-indexed userErrors mapping is incompatible with `write_gate`'s fail-fast model.
  - `catalog_hygiene.{update_product_category,update_product_vendor,update_product_type,update_product_pricing}` — use `_format_payload()` JSON-tail output format (incompatible with `write_gate`'s string return) and different confirm hint text ("Reply with confirm=True to execute." vs "To apply, call again with confirm=True.").
- **Intentionally NOT migrated — complex control flow:**
  - `products.update_variant_inventory_policy` — custom dotted-field-path error formatter
  - `collections.{add,remove}_product_to_collection` — async job polling via `poll_job()`
  - `inventory.{update_variant_inventory_tracking,update_variant_inventory_quantity}` — per-variant try/except isolation
  - `discounts.create_discount_code` — two-stage mutation with `priceRuleUserErrors` custom key
  - `catalog_hygiene.{set,delete}_product_metafields`, `update_variant_image_binding`, `update_product_options` — multi-step orchestration, JSON-tail format
- **Business justification:** write surface is the highest-risk part of the server (irreversible Shopify mutations). Centralising the safety scaffolding is worth more than just LOC reduction.

### A3 — Pagination helper for list reads

- **Category:** Code
- **Status:** helper shipped — `ShopifyClient.paginate()` at [shopify_client.py:224](shopify_client.py:224), tested in [tests/unit/test_paginate.py](tests/unit/test_paginate.py), mirrored in `_testing/fake_client.py`. Every cleanly-paginable single-object read has adopted it (inventory + media under Story 10.6; orders, products, publications under Story 10.16). Remaining gaps are the two structural exceptions below.
- **Impact (2):** prevents silent truncation on stores with >50 variants per product or >100 media per product.
- **Risk (3):** the helper-adopted read paths now auto-continue across pages. The residual risk is the two connections `paginate()` structurally cannot walk — both documented, note-only items in tech-debt.md (`A3-orders-lineitems-cap`, `A3-option-echo-cap`): `GET_ORDERS.nodes.lineItems` (a connection nested inside a list, so `get_orders` still caps per order) and the `UPDATE_PRODUCT_OPTION` mutation-response echo (mitigated by a pre-write at-cap warning).
- **Effort (3):** historical estimate (~half a day). The helper and the read-path sweep are done; only the two structural exceptions remain, and neither is addressable by `paginate()` as designed.
- **Plan:** ✅ delivered. `paginate(query, variables, *, connection_path, page_size=50, max_pages=10)` walks `pageInfo.hasNextPage` / `endCursor`, hard-capping `max_pages` to prevent runaway calls and returning a `capped` flag so tools surface a visible warning. Tools at risk of the cap migrated; tools that never approach it stayed as-is. The list-nested and mutation-echo connections are out of scope by construction.
- **Business justification:** silent data truncation in a tool that mutates Shopify state is the worst possible failure mode — user thinks they updated all variants, only the first 50 changed.
- **Shipped this session (Story 10.16):**
  - `orders.get_order` — `GET_ORDER_BY_ID` migrated to `client.paginate()` with `connection_path=["order", "lineItems"]`, page_size=50. Warns when max-pages cap is hit.
  - `products.get_product` — `GET_PRODUCT_BY_ID` / `GET_PRODUCT_BY_HANDLE` migrated to `client.paginate()` with `connection_path=["product"|"productByHandle", "variants"]`. Queries use `$first: Int = 50` default so non-paginating callers (`update_product_title`, `update_product_description`, `update_product_status`, `get_product_description`) remain unaffected.
  - `products.get_product_full` — `GET_PRODUCT_FULL_BY_ID` / `GET_PRODUCT_FULL_BY_HANDLE` migrated similarly. Same `$first: Int = 50` default.
  - `products.update_variant_inventory_policy` — `GET_PRODUCT_VARIANTS_POLICY` migrated with page_size=250 (full policy sweep). Real `capped` flag replaces the old `len()>=250` heuristic.
  - `publications._load_channels` — `LIST_PUBLICATIONS` migrated to `client.paginate()` with `connection_path=["publications"]`.
  - `publications._resolve_product_gid_and_meta` — `GET_PRODUCT_PUBLICATIONS_BY_ID` / `GET_PRODUCT_PUBLICATIONS_BY_HANDLE` migrated with `connection_path=["product"|"productByHandle", "resourcePublications"]`.
  - `catalog_hygiene.update_product_options` — warn path (not full pagination): `pageInfo{hasNextPage}` added to `GET_PRODUCT_OPTIONS*`, at-cap warning emitted when product has >50 variants. Closes T-9.5-variants-cap.

### A5 — `shopify/` subpackage extraction

- **Category:** Architecture
- **Impact (2):** unblocks query/operation reuse, separates business logic from MCP-tool surface, makes operations testable from non-MCP entry points (CLI, scripts).
- **Risk (1):** no active pain at 8 domains; risk grows with each new domain added without restructuring.
- **Effort (2):** ~1 day. Tool registration stays put; only business logic moves.
- **Plan:** three thin layers — `shopify/queries/` (GraphQL strings, grouped by resource, reusable via fragments), `shopify/operations/` (typed wrappers like `update_product_title(client, id, title) -> dict`), and `tools/` (param coercion, preview/confirm flow, formatting). Pair with GraphQL fragment extraction so `GET_PRODUCT_BY_ID` and `GET_PRODUCT_BY_HANDLE` share their selection set.
- **Business justification:** worth doing **before** the codebase grows past ~12 domains, not after. Mechanical restructuring is cheap at small scale and exponentially more expensive once dependencies have accumulated.
- **Status (✅ closed — Story 10.31; the full sweep landed across `products` pilot Story 10.23, `catalog_hygiene` Story 10.25, `collections` Story 10.26, `discounts` Story 10.27, `inventory` Story 10.28, `orders` Story 10.29, `publications` Story 10.30, `webhooks` Story 10.31):**
  the `shopify/` package and the three-layer structure are **established**, with all eight domains —
  `products`, `catalog_hygiene`, `collections`, `discounts`, `inventory`, `orders`, `publications`, and `webhooks` — migrated:
  - `shopify/queries/products.py` holds all product GraphQL strings; shared
    fragments `ProductCoreFields` / `ProductFullFields` dedup the by-id and
    by-handle selection sets.
  - `shopify/operations/products.py` holds typed wrappers that take a duck-typed
    `GraphQLClient` (`shopify/_client.py`) and are callable without FastMCP;
    `tools/products.py` now delegates to them and keeps only coercion +
    preview/confirm + formatting.
  - `shopify/queries/catalog_hygiene.py` holds the catalog-hygiene GraphQL
    strings + the dynamic metafield/batch query builders; shared fragments
    `ProductVendorFields` / `ProductTypeFields` / `ProductOptionsFields` dedup
    the by-id and by-handle pairs. `shopify/operations/catalog_hygiene.py` holds
    the typed read/mutation wrappers; `tools/catalog_hygiene.py` re-exports the
    query constants + `_build_*` builders via `__all__` and delegates every
    `client.execute` to the operations layer (behavior-preserving — the existing
    `test_catalog_hygiene_offline.py` passes unedited).
  - `shopify/queries/collections.py` holds the collections GraphQL strings;
    `shopify/operations/collections.py` holds the typed read/mutation wrappers
    (read-by-handle, `collectionUpdate`, and the add/remove membership mutations
    with `Product` GID coercion). `tools/collections.py` re-exports the query
    constants via `__all__` and delegates every `client.execute` to the
    operations layer, keeping only smart/manual classification, preview/confirm,
    async job polling, and formatting (behavior-preserving — the existing
    `test_collections_offline.py` passes unedited). **No shared fragment** is
    extracted: collections has a single by-handle read and no by-id twin, so no
    duplicated selection set exists — fragment dedup is opportunistic and did not
    apply here (Story 10.26 / A5, AC3).
  - `shopify/queries/discounts.py` holds the discounts GraphQL strings
    (`GET_PRICE_RULES`, `CREATE_PRICE_RULE`, `CREATE_DISCOUNT_CODE`);
    `shopify/operations/discounts.py` holds the typed read/mutation wrappers
    (price-rule list read + the two-step `priceRuleCreate` / discount-code
    create). `tools/discounts.py` re-exports the query constants via `__all__`
    and delegates every `client.execute` to the operations layer, keeping only
    param coercion, the PriceRuleInput assembly, preview/confirm, and formatting
    (behavior-preserving — the existing `test_discounts_offline.py` passes
    unedited). **No shared fragment** is extracted: discounts has no
    by-id/by-handle pair and its three selection sets do not overlap, so no
    duplicated selection set exists (Story 10.27 / A5, AC3).
  - `shopify/queries/inventory.py` holds the inventory GraphQL strings
    (`GET_PRODUCT_INVENTORY`, `UPDATE_INVENTORY_ITEM_TRACKED`, `GET_INVENTORY_ITEM`,
    `SET_INVENTORY`); `shopify/operations/inventory.py` holds the typed
    read/mutation wrappers (product-inventory + inventory-item reads, the
    `inventoryItemUpdate` tracking toggle, and the `inventorySetOnHandQuantities`
    write). `tools/inventory.py` re-exports the query constants via `__all__` and
    delegates every `client.execute` / `client.paginate` to the operations layer,
    keeping only param coercion, the (variant, location) bucketing, preview/confirm,
    and formatting (behavior-preserving — the existing `test_inventory_offline.py`
    passes unedited). **A shared fragment applies:** the two reads
    (`GET_PRODUCT_INVENTORY`, `GET_INVENTORY_ITEM`) spread one
    `InventoryLevelQuantities` fragment for the 2024-07+
    `quantities(names: ["available"])` selection they duplicate, while each keeps
    its own `location` selection (Story 10.28 / A5, AC3).
  - `shopify/queries/orders.py` holds the orders GraphQL strings (`GET_ORDERS`,
    `GET_ORDER_BY_ID`); `shopify/operations/orders.py` holds the typed read
    wrappers (the orders list read + the single-order read whose line items
    paginate via `client.paginate`), doing the `Order` GID coercion there.
    `tools/orders.py` re-exports the query constants via `__all__` and delegates
    every `client.execute` / `client.paginate` to the operations layer, keeping
    only limit clamping, the untrusted-data wrapping, and formatting
    (behavior-preserving — the existing `test_orders_offline.py` passes unedited).
    **A shared fragment applies:** the two reads spread one `OrderCoreFields`
    fragment for the `id name createdAt totalPriceSet { shopMoney { amount } }`
    core they duplicate, while each keeps its own traffic-source, status, and
    line-item selections (Story 10.29 / A5, AC3). orders is read-only — no
    mutation wrappers.
  - `shopify/queries/publications.py` holds the publications GraphQL strings
    (`LIST_PUBLICATIONS`, `GET_PRODUCT_PUBLICATIONS_BY_ID`,
    `GET_PRODUCT_PUBLICATIONS_BY_HANDLE`, `PUBLISHABLE_PUBLISH`,
    `PUBLISHABLE_UNPUBLISH`); `shopify/operations/publications.py` holds the typed
    read/mutation wrappers (the publications list read, the by-id/by-handle
    resourcePublications read, and the `publishablePublish` / `publishableUnpublish`
    writes whose `[{"publicationId": ...}]` input it builds), doing the `Product`
    GID coercion there. `tools/publications.py` re-exports the query constants via
    `__all__` and delegates every `client.execute` / `client.paginate` to the
    operations layer, keeping only the channel-name/-id resolution cache, the
    publish/unpublish/declarative-set diff, preview/confirm, userError mapping, and
    formatting (behavior-preserving — the existing `test_publications_offline.py`
    passes unedited). **A shared fragment applies:** the by-id and by-handle reads
    differ only in their root field, so the whole `Product` selection (`id title
    handle` + the paginated `resourcePublications`) is one `ProductPublicationsFields`
    fragment both spread (Story 10.30 / A5, AC3).
  - `shopify/queries/webhooks.py` holds the webhooks GraphQL strings
    (`LIST_WEBHOOKS`, `CREATE_WEBHOOK`, `DELETE_WEBHOOK`);
    `shopify/operations/webhooks.py` holds the typed read/mutation wrappers (the
    webhook-subscription list read + the `webhookSubscriptionCreate` /
    `webhookSubscriptionDelete` writes, doing the `WebhookSubscription` GID coercion
    there). `tools/webhooks.py` re-exports the query constants via `__all__` and
    delegates every `client.execute` to the operations layer, keeping only the
    endpoint-allowlist validation, preview/confirm, audit logging, and formatting
    (behavior-preserving — the existing `test_webhooks_offline.py` passes unedited).
    **No shared fragment** is extracted: webhooks has no by-id/by-handle pair (one
    list read + two mutations, the same shape as discounts), and the only recurring
    selection — the small `endpoint { __typename ... on WebhookHttpEndpoint
    { callbackUrl } }` union block shared by the list read and the create mutation —
    is a micro sub-selection, not an entity core across a read pair, so it is left
    inline exactly as orders left its `{ shopMoney { amount } }` money block
    (Story 10.31 / A5, AC3).
  - The one-way rule (`shopify/` never imports `tools/`) is enforced by
    `tests/architecture/test_layering.py`.
  - **Q3-helper decision:** the GID helpers moved to `shopify/_ids.py` (the
    operations layer needs `to_gid` and must not import `tools/`); `tools/_gid.py`
    is now a thin re-export shim so existing `from tools._gid import ...` call
    sites are unchanged. `tools/_response.py` **stays in `tools/`** — its helpers
    (`with_confirm_hint`, `extract_user_errors`) are preview/response-formatting
    concerns used by the tool layer; revisit if a `shopify` operation ever needs
    `extract_user_errors`.
  - **Done:** `webhooks` — the last domain — migrated in Story 10.31, **closing A5**.
    The original six-domain sweep (collections, discounts, inventory, orders,
    publications, webhooks) plus the `products` pilot and `catalog_hygiene` are now
    all under `shopify/`.
    Effort note: the full sweep was larger than the original ~1-day estimate
    (catalog_hygiene alone was ~4,600 lines / ~99 GraphQL blocks), hence the
    incremental one-domain-per-PR approach.

### A6 — HTTP client unification

- **Category:** Architecture
- **Status:** ✅ **closed — Story 10.24.** Both halves done. The **policy** half (shared User-Agent + config-driven timeouts across both stacks) closed under **N4 / Story 10.21 (PR #87)**. This story closed the **transport** half: `ShopifyClient.fetch_bytes(url, *, max_size, allow_redirects=False)` now wraps the image-download GET with the SSRF guard, shared headers, the `Settings.download_timeout_s` timeout, a streaming size cap, redirect refusal, and retry on retryable statuses (429/5xx). The gql retry loop and `fetch_bytes` now share **one** backoff implementation (`ShopifyClient._with_retry`), so there is no duplicated retry logic. The staged-upload PUT deliberately stays a single-shot `requests.put` (non-idempotent large signed upload — see the grooming decision on the Story 10.24 card and the code comment in `_upload_bytes_to_target`); it still shares the HTTP *policy* via `default_headers`, only automatic retry is excluded.
- **Cross-reference:** tech-debt.md item **N4** (watch). N4's trigger is "a second tool starts using `requests` directly." A6 is the architectural framing of the same concern.
- **Impact (2):** one retry policy, one timeout config, one User-Agent. Foundation for A1's retry/backoff to apply uniformly.
- **Risk (2):** today [tools/media/_upload.py:18](tools/media/_upload.py:18) uses `requests` directly for image downloads alongside `gql`'s `RequestsHTTPTransport`. Two stacks means two failure modes the user has to learn.
- **Effort (3):** ~half a day. `client.fetch_bytes(url, max_size=...)` wrapper exposed off `ShopifyClient`.
- **Plan:** unify under a single client wrapper exposing both GraphQL execution and arbitrary HTTP fetches. Image download in `tools/media/_upload.py` becomes `client.fetch_bytes(url, max_size=...)`. Pairs naturally with A1 (shared retry policy across both).
- **Business justification:** rolls together with A1 — once the throttle-aware policy exists, having two HTTP stacks means only half of calls benefit.

### A10 — Committed `uv.lock`

- **Category:** Dependency
- **Impact (1):** CI reproducibility. Today CI and dev runs may pull different patch versions of `gql`, `requests`, `mcp`.
- **Risk (1):** very low — for a server that talks to a versioned API, the existing `>=floor,<next-major` bounds catch the dangerous drift.
- **Effort (5):** ~5 minutes. `uv lock` and commit the resulting file.
- **Plan:** float major versions in `pyproject.toml`, freeze exact versions in `uv.lock`. Resolves the small CI-vs-dev reproducibility smell without sacrificing dev experience.
- **Business justification:** lowest-priority item on the list. Do only when paired with another change touching `pyproject.toml`, or after a real CI-vs-dev divergence.

---

## Phased remediation plan

Designed to interleave with feature work, not block it. No phase is more than ~3 days of focused effort.

### Phase 1 — Foundational (complete)

~~**A1** — closed, PR #70~~
~~**A4** — closed, branch `claude/elated-kirch-43a66a`~~
~~**A7** — closed, branch `claude/relaxed-hertz-8f050d`~~

### Phase 2 — Tool surface (complete)

~~**A2** — closed, Story 10.22. 9 tools use `write_gate()`; publications and catalog_hygiene standard tools deliberately excluded (incompatible control flow — see A2 item).~~
~~**A3** — closed, Story 10.16. `paginate()` helper shipped; all cleanly-paginable reads migrated.~~

### Phase 3 — Restructure (do before reaching ~12 domains or starting multi-store work, ~3 days)

| Day | Item | Why |
|-----|------|-----|
| 1–2 | ~~**A5** `shopify/` subpackage~~ *(closed — Story 10.31; the structure + `products` pilot landed in Story 10.23, then `catalog_hygiene` (10.25), `collections` (10.26), `discounts` (10.27), `inventory` (10.28), `orders` (10.29), `publications` (10.30), and `webhooks` (10.31) migrated one domain per PR)* | Restructure before the codebase grows past the size where mechanical reshuffling is cheap. |
| 2–3 | ~~**A6** HTTP unification~~ *(closed — Story 10.24; `client.fetch_bytes()` + shared `_with_retry`. Policy half was N4/Story 10.21.)* | Pairs naturally with A5; closes tech-debt.md N4. |

### Phase 4 — Repository layout (complete)

Added by the 2026-07-25 folder-structure audit and delivered the same day. Phase 3 restructured the code *inside* the repo; this phase restructured the repo itself. The three sequential items landed in order, each on the previous one's tree.

| Day | Item | Why |
|-----|------|-----|
| 1 | ~~**A11** test suite into `tests/`~~ *(closed — Story 10.45)* | Largest single readability win (root went 62 → 22 tracked entries), and A12 needed `tests/` to be an importable package before it could move. |
| 1 | ~~**A12** un-ship `_testing/`~~ *(closed — Story 10.46)* | Small, and the natural tail of A11. |
| 2–3 | ~~**A13** `src/` layout~~ *(closed — Story 10.47)* | Done last: it rewrote imports across ~110 files, so every earlier move was already settled. Closes the `shopify` ↔ ShopifyAPI collision. |

~~**A14** (docs consolidation)~~ *(closed — Story 10.48)* — independent of the other three; edited README.md, which A13 also touched (A11 had already landed its README changes).

**Ordering held up in practice.** The one real cross-item constraint surfaced mid-phase rather than at planning time: Story 10.50 (ruff/mypy target alignment) edited `depcheck.py`, `tools/_log.py`, and `tools/discounts.py`, all of which A13 then relocated under `src/`, so it had to land *before* A13 or be redone against moved files. Sequencing a config-only change ahead of a large move is the transferable lesson.

### Backlog (don't pre-refactor)

| Item | Trigger |
|------|---------|
| ~~**A8** Caching~~ *(closed — Story 10.32; channels-only cross-call TTLCache, implemented ahead of trigger)* | ~~Call volume rises, or first real Shopify quota miss.~~ |
| **A10** Lockfile | First CI-vs-dev divergence caused by a floated dep. |

---

## Closed

### A11 — Test suite location and structure *(closed Story 10.45)*

- **Category:** Test
- **Source:** 2026-07-25 folder-structure audit. Tracked as [Story 10.45](https://trello.com/c/0KMraglI) (filed on the card as `FS-1`).
- **Closed:** 2026-07-25, Story 10.45 — *relocate test suite into `tests/` with a source-mirroring layout*.
- **The debt:** the repo root held 62 tracked entries, 39 of them test modules (~28k lines, over 80% of the codebase by line count). `git status`, tab-completion, and any file tree were dominated by tests. Three smells compounded it: the `_offline` suffix on 36 files encoded "needs no live credentials" into the filename; the live/offline split was expressed *twice* — once in filenames and again as a hand-maintained `addopts = "--ignore=test_shopify_mcp.py --ignore=test_webhooks.py"` list that had to be edited for every new live runner; and 8 resources had two test files each, disambiguated by an `_operations` infix instead of by directory.
- **What shipped:** all 39 modules moved under `tests/` (via `git mv`, history preserved) into `tests/{unit/{tools,shopify/operations,validators},architecture,live}/`, mirroring the source tree. The `_offline` suffix and the `_operations` infix are both gone — the directory carries that meaning now. Three CWD-fragile guards were anchored to the repo root. Root dropped from 62 tracked entries to 22. `tests/architecture/test_layout.py` is the new executable spec for the whole invariant.
- **Deviation from the plan above — read this before "simplifying" the config.** The plan called for `testpaths` + `--ignore=tests/live`. Only the first half shipped that way. pytest resolves a *relative* `--ignore` against the **invocation directory**, not the rootdir, so `cd /tmp && pytest ~/shopify-mcp` silently stopped ignoring `tests/live` and failed five tests on absent credentials — verified, not theoretical. The exclusion therefore lives in `tests/conftest.py` as `collect_ignore = ["live"]`, which pytest resolves relative to the declaring conftest and which holds from any working directory. `pytest tests/live` still works as the deliberate escape hatch. `pyproject.toml` carries `testpaths = ["tests"]` and no `addopts` at all.
- **Gotcha worth remembering:** mirroring the source layout produces nine duplicate test basenames. Under pytest's default *prepend* import mode those are a hard collection error ("import file mismatch"), not a warning — **every** directory under `tests/` needs an `__init__.py`. The layout guard asserts this so the next person hits a readable assertion instead.
- **Unblocks:** A12 — `tests/` is now an importable package, which is what the `_testing/` doubles need before they can move.

---

### A12 — `_testing/` ships inside the distribution *(closed Story 10.46)*

- **Category:** Architecture
- **Closed:** 2026-07-25, Story 10.46 ([PR #116](https://github.com/rcchirwa/shopify-mcp/pull/116)) — *move test doubles out of the shipped distribution*.
- **Status:** ✅ **closed — Story 10.46.** The doubles now live at `tests/support/` (`git mv`, history preserved) and `_testing` is gone from `[tool.setuptools] packages`. The mypy `disallow_untyped_defs` gate moved with the code to `tests/support` in `[tool.mypy] files` rather than being dropped. Verified per the note below: a fresh `pip install -e .` followed by `python -c "import _testing"` raises `ModuleNotFoundError`.
- **Source:** 2026-07-25 folder-structure audit. Tracked as [Story 10.46](https://trello.com/c/9alOE7U4) (filed on the card as `FS-2`). **Depended on A11, which closed with Story 10.45 — this is now unblocked.**
- **Impact (2):** `_testing` is listed in `[tool.setuptools] packages`, so `pip install -e .` — and any real wheel install — puts the test doubles (`FakeClient`, `CapturingServer`) into site-packages beside production code. Anyone installing this MCP server also installs its test fixtures.
- **Risk (2):** the packaging declaration contradicts the repo's own stated intent. `_testing` is already excluded from `[tool.coverage.run] source` with the comment "test doubles exercised only through the offline suites, not production code worth gating on." One of the two declarations is wrong, and it is the shipping one.
- **Effort (4):** ~1–2 hours. 18 import sites plus three config edits.
- **Plan:** `git mv _testing tests/support`, rewrite the 18 imports, drop `_testing` from `[tool.setuptools] packages`. Preserve the existing mypy `disallow_untyped_defs` gate on the doubles by adding `tests/support` to `[tool.mypy] files` — moving the code must not silently drop its type gate.
- **Verification note:** confirm by attempting `import _testing` after a fresh editable install, not by reading the config diff. A packaging change that looks right and isn't is exactly the failure this item is about.
- **Business justification:** small, but it is the difference between a distribution that is what it claims to be and one that leaks its own test scaffolding to consumers.

---

### A13 — `src/` layout under a single `shopify_mcp` package *(closed Story 10.47)*

- **Category:** Architecture
- **Closed:** 2026-07-25, Story 10.47 ([PR #117](https://github.com/rcchirwa/shopify-mcp/pull/117)) — *adopt `src/` layout under a single `shopify_mcp` package*.
- **Status:** ✅ **closed — Story 10.47.** The tree is now `src/shopify_mcp/` (`git mv`, history preserved) holding `server.py`, `client.py`, `settings.py`, `logging_config.py`, `depcheck.py` and the `shopify/`, `tools/`, `validators/` sub-packages, plus a new `__main__.py` replacing the old `python shopify_mcp.py` invocation. `[tool.setuptools]` declares `package-dir = {"" = "src"}` with a `find` directive scoped to `src`, so the explicit `py-modules`/`packages` lists are gone. Done as a single stage, not the two the plan suggested: the back-compat shims (`tools/_gid.py`) turned out to be orthogonal to the move, so splitting would have added a second review of the same files for no risk reduction — they stay for A5's separate cleanup. Verified by running, not by reading the diff: the `shopify-mcp` console script launched from `CWD=/tmp` completes an MCP `initialize` handshake and registers all 47 tools, and a `log_write` from a foreign CWD lands at the repo-root `aon_mcp_log.txt` with nothing written under `src/`. A wheel built from the tree contains exactly one top-level entry (`shopify_mcp/`); installed into a clean venv **alongside `ShopifyAPI`**, `import shopify` resolves to the third-party library while this project's domain layer is reachable only as `shopify_mcp.shopify` — the collision is closed. All six pre-move top-level names (`tools`, `validators`, `settings`, `logging_config`, `depcheck`, `shopify_client`) are absent from that env.
- **Correction to the estimate below:** the landmine count was **41 string-literal mock targets, not 32.** The audit's figure came from `grep -rn 'patch("' tests/`, which counts *lines*; nine targets sit on a continuation line of a multi-line `patch(` call and were invisible to it. Sweeping for the literal (a quoted pure-dotted-path) rather than the call found all 41. Anyone repeating this kind of rename should sweep for the string, not the call site.
- **Known limitation (pre-existing in kind; the failure mode shifted):** `depcheck._PYPROJECT`, `tools/_log.py`'s `LOG_FILE`, and the two `_ENV_PATH` constants are all derived from `__file__`, so they only resolve correctly in a source tree / editable install — the layout `depcheck`'s docstring already scopes itself to, and the only one the documented setup uses. Under a *wheel* install they were already wrong (they resolved into `site-packages/`), but they are now wrong one directory further out, in `<prefix>/lib/pythonX.Y/`. That is worth stating precisely rather than as "the contract did not change": on a root-owned system prefix the audit log's `RotatingFileHandler` will now raise at construction instead of writing a stray file, which surfaces as an error from every write tool. Not reachable from the documented editable install, and not fixed here because this story is behavior-preserving — but the two obvious fixes are both wrong for this codebase and worth recording so they are not re-proposed: a CWD-relative anchor contradicts the very constraint these constants exist to satisfy (Claude Desktop launches with `CWD=/`), and making a missing `.env` a hard failure would break the documented Claude Desktop setup, which supplies credentials through the launcher's `env` block with no `.env` on disk at all. An explicit env-var override is the viable option, as a follow-up.
- **Enforced by:** `tests/architecture/test_src_layout.py` — pins the layout, the packaging and tool configs, the absence of legacy imports and stale mock targets, and (via a clean-interpreter subprocess) that no legacy top-level name resolves back into this repo.
- **Source:** 2026-07-25 folder-structure audit. Tracked as [Story 10.47](https://trello.com/c/1zmUKlY0) (filed on the card as `FS-3`). **Depends on A12; A11 closed with Story 10.45.**
- **Impact (3):** `[tool.setuptools]` installs six generic top-level names — `depcheck`, `logging_config`, `settings`, `shopify_client`, `shopify_mcp`, plus the `tools`, `validators`, and `shopify` packages — onto the import path of any environment that installs this project. Collapsing to one owned name (`shopify_mcp`) removes the whole class of conflict.
- **Risk (3):** **`shopify` is the top-level module owned by the `ShopifyAPI` distribution on PyPI** — the mainstream Python Shopify library, which anyone working on a Shopify project is plausibly one `pip install` away from. Installed together, two different `shopify` packages contend on `sys.path`; resolution depends on path order and the failure mode is a silent wrong import, not an error. Separately, the flat layout means `pytest` from the repo root imports the working tree rather than the installed distribution — so a module omitted from `[tool.setuptools] packages` passes the entire CI gate and breaks only for someone doing a real install. `src/` layout is the standard fix for that second problem: the working tree is not importable, so tests exercise what actually ships.
- **Effort (2):** multi-day, ~110 files. Highest-churn item in this batch.
- **Plan:** `src/shopify_mcp/` holding `server.py` (was `shopify_mcp.py`), `client.py` (was `shopify_client.py`), `settings.py`, `logging_config.py`, `depcheck.py`, and the `shopify/`, `tools/`, `validators/` sub-packages. Recommended in two stages — move and get green first, then collapse the `tools/_gid.py`-style back-compat shims — because all the risk sits in stage one.
- **Landmines (verified — each survives a naive rename):** 32 string-literal `patch("tools…")` / `patch("shopify_client…")` mock targets, invisible to mypy and ruff, where a stale target yields a **passing test that patches nothing**; `tools/_log.py`'s `LOG_FILE`, which would start writing the write-audit log to `src/`; `depcheck.py`'s `_PYPROJECT`; and the `_ENV_PATH` constants in both `shopify_mcp.py` and `shopify_client.py`, where an error means the server boots with no credentials. `[project.scripts]` entry points change too, and README's Claude Desktop registration section points at `.venv/bin/shopify-mcp`.
- **Business justification:** the namespace collision is latent rather than active — nothing installs ShopifyAPI today — but it is a silent-wrong-answer failure in a project whose entire domain is Shopify, and the cost of fixing it only grows with the codebase. The src-layout half pays for itself the first time a packaging omission would otherwise reach a user.
---

### A14 — Root markdown consolidated under `docs/` *(closed Story 10.48)*

- **Category:** Documentation
- **Source:** 2026-07-25 folder-structure audit. Tracked as [Story 10.48](https://trello.com/c/M7E2dr8A) (filed on the card as `FS-4`).
- **Closed:** 2026-07-25, Story 10.48 — *move both ledgers into `docs/` and sweep every reference*.
- **The debt:** 121KB of markdown sat at the repo root — the tactical ledger (87KB) and this file (34KB) — while `docs/` existed and held exactly one spec. A `docs/` directory holding one file while the two largest documents sat outside it taught the wrong convention to the next contributor.
- **What shipped:** both ledgers moved via `git mv` (history preserved) to `docs/tech-debt.md` and `docs/architecture-tech-debt.md`, kebab-case to match the existing `docs/specs/` naming. Every reference to the old root filenames — in both ledgers, `docs/specs/story-10.19-sec-resolver-reflect-cap.md`, two source comments, and two architecture-guard tests — was repointed. README's project-structure tree now shows the `docs/` directory.
- **Business justification:** lowest-priority item alongside A10; landed opportunistically rather than blocking on it.

---

### A8 — Metadata `TTLCache` (channels-only) *(closed Story 10.32, branch `claude/trusting-chaum-3a8f21`)*

- **Category:** Code
- **Closed:** 2026-06-23, branch `claude/trusting-chaum-3a8f21` (Story 10.32 / A8)
- **Scope correction:** the original A8 framing — "locations / channels / shop info / metafield definitions re-resolved on every call" — was **verified inaccurate**. Only **publication channels** are read cross-call. Locations appear only as a *nested field inside inventory queries* ([shopify/queries/inventory.py](shopify/queries/inventory.py) lines 45, 80), never as a standalone cacheable list; shop info and metafield definitions are **not read anywhere** in the codebase. The "dead `channel_cache`" wording was likewise imprecise — the `channel_cache` dict in [tools/publications.py](tools/publications.py) is **live per-call** (it memoizes within one invocation) but was rebuilt every call; what was missing is a *persistent, TTL'd* cache across calls, which this story adds. Groomed scope = **channels-only**; the other three resources are deferred until a real cross-call read for each exists.
- **What shipped:** [shopify/_cache.py](shopify/_cache.py) — `ShopifyMetadataCache`, a registry of per-resource-type `cachetools.TTLCache` buckets keyed by resource type (only `channels` wired today; a second resource is a one-line bucket plus its Settings TTL field). Attached as `ShopifyClient._metadata_cache`, mirrored on `_testing/fake_client.py`. [tools/publications.py](tools/publications.py) `_load_channels` reads `LIST_PUBLICATIONS` through the cross-call cache (`_read_channels`), layered above the existing per-call `channel_cache` index; a channel-name miss forces a cache-bypassing refresh (`force=True`) so a changed roster is still discovered. `publish` / `unpublish` / `set_product_publications` invalidate the channels bucket via `_invalidate_channels`. `settings.py`: `cache_ttl_channels_s: int = 600` (TTL is Settings-driven, beside the reserved-but-unused `cache_ttl_locations_s`). `cachetools>=5,<6` added to `pyproject.toml` dependencies; `.env.example` documents the `CACHE_TTL_CHANNELS_S` knob.
- **Deferred (no reads exist yet):** locations / shop-info / metafield-definition buckets — added when a real cross-call read for each lands. Keeping them out avoids speculative dead cache wiring.
- **Test footprint:** `test_metadata_cache_offline.py` (new — cache unit tests incl. TTL expiry via injected clock) plus cross-call cache-hit, TTL-expiry-through-read, and write-invalidation tests in `test_publications_offline.py`; 100% coverage gate held; mypy clean; ruff clean.

### A7 — `Settings` class via `pydantic-settings` *(closed branch `claude/relaxed-hertz-8f050d`)*

- **Category:** Architecture
- **Closed:** 2026-05-23, branch `claude/relaxed-hertz-8f050d`
- **What shipped:** `settings.py` with `Settings(BaseSettings)` exposing credentials (`shopify_store_url`, `shopify_access_token: SecretStr`, `shopify_api_version`), HTTP/retry/poll knobs (`request_timeout_s`, `job_poll_timeout_s`, `retry_max_attempts`, `retry_base_s`, `retry_cap_s`, `poll_base_s`, `poll_cap_s`), webhook allowlist (`webhook_allowlist_hosts` + `webhook_allowlist_set` computed property), and reserved fields for A4/A8 (`log_level`, `log_format`, `cache_ttl_locations_s`). Pydantic field validators on `shopify_store_url` (regex `<shop>.myshopify.com`) and `shopify_api_version` (regex `YYYY-MM`); warn-only stderr print when token does not start with `shpat_`. `ShopifyClient(settings: Settings | None = None)` lets tests pass a custom Settings without monkeypatching module constants. Promoted constants (`JOB_POLL_TIMEOUT_S`, `_RETRY_*`, `_POLL_*`) deleted from `shopify_client.py`; `tools/collections.py`, `tools/media/_reorder.py`, `tools/media/_upload.py`, and `tools/webhooks.py` migrated to read from `client._settings`. `_testing/fake_client.py` carries a default Settings so tool offline tests work without a real `.env`.
- **Deviation from original plan:** `job_poll_timeout_s` default kept at `10.0` instead of the doc's `60.0` — `poll_job` is informational (the mutation has already succeeded), so 10s gives the user faster feedback for a job that already worked.
- **Test footprint:** 925 offline tests pass; 100% coverage gate held (6 new tests in `test_settings_offline.py` cover the validator failure branches and the `webhook_allowlist_set` parsing).

### A4 — Stdlib `logging` adoption *(closed branch `claude/elated-kirch-43a66a`)*

- **Category:** Infrastructure
- **Closed:** 2026-05-25, branch `claude/elated-kirch-43a66a`
- **What shipped:** `logging_config.py` (new) — `configure_logging(settings: Settings) -> None` with `StreamHandler(sys.stderr)`, text formatter (`%(asctime)s %(levelname)s %(name)s %(message)s`) or JSON formatter (`pythonjsonlogger.json.JsonFormatter`) selected by `settings.log_format`; idempotent via module-level `_configured: bool` flag (not `if root.handlers` — pytest attaches its own `LogCaptureHandler` subclasses, which would cause the handler-count guard to fire immediately). `settings.py` `log_level` field promoted from `str` to `Literal["DEBUG","INFO","WARNING","ERROR","CRITICAL"]` for parity with `log_format`'s `Literal` constraint; `getattr` fallback removed from `configure_logging`. `shopify_client.py`: removed `import sys` and `_backoff_sleep()` (inlined as `delay = _backoff_delay(...)` + `logger.warning(...)` + `time.sleep(delay)` in both retry branches so the sleep duration appears in the warning log); added `logger = logging.getLogger(__name__)`; `configure_logging(self._settings)` called in `__init__()` after settings resolved; bare `print(..., file=sys.stderr)` fingerprint replaced with `logger.info("store=%s ...", ...)`; `logger.debug("gql op=%s variables=%s", op_name, list(variables.keys()))` before retry loop (variable keys only — never values); `logger.warning("throttled ...")` and `logger.warning("retryable_http ...")` on each retry sleep. `shopify_mcp.py`: `logger = logging.getLogger(__name__)` + `logger.info("shopify-aon MCP server initialized")` in `create_server()`. `conftest.py`: `_reset_root_logger` autouse fixture — teardown resets `_configured = False` and removes `type(h) is logging.StreamHandler` handlers from root. `test_logging_config_offline.py` (new): 7 tests covering text formatter, JSON formatter, idempotency, root level propagation, DEBUG emit, DEBUG suppression, and audit logger `propagate` non-mutation. `pyproject.toml`: `python-json-logger>=3,<4` dep; `logging_config` added to `py-modules`, `[tool.coverage.run] source`, and `[tool.mypy] files`.
- **Test footprint:** 936 offline tests pass; 100% coverage gate held (3201 statements); mypy clean (34 files); ruff clean.
- **Design decisions:** all output to `sys.stderr` (stdout is the MCP JSON-RPC channel); `_configured` flag beats `if root.handlers` for idempotency under pytest; variable values never logged (only keys) to prevent PII/product-data leakage; `pythonjsonlogger.json.JsonFormatter` lazy-imported only when `log_format="json"`; audit logger `shopify_aon.audit` untouched — its `RotatingFileHandler` and `propagate=False` remain owned by `tools/_log.py`.

### A1 — Throttle-aware `ShopifyClient.execute()` with retry/backoff and cost tracking *(closed PR #70)*

- **Category:** Architecture
- **Closed:** 2026-05-23, PR #70 (`fix(security): M5 — throttle-aware backoff in ShopifyClient.execute()`)
- **What shipped:** `TransientShopifyError` / `ShopifyError` error taxonomy; `_is_throttled` / `_is_retryable_http` classifiers; capped exponential backoff with jitter via `_backoff_sleep`; ≤5-attempt retry loop on `THROTTLED`, 429, and 5xx; `poll_job` switched to exponential backoff (start `_POLL_BASE_S`, cap `_POLL_CAP_S`). Retry knobs are module-level constants (A7 can promote them to env-configurable once the Settings class lands).
- **Original plan:** parse `extensions.cost.throttleStatus.currentlyAvailable`; sleep until the bucket has capacity; retry on `THROTTLED`, 429, 5xx with capped exponential backoff + jitter; bound retries (≤5); categorize errors as `TransientShopifyError` vs `ShopifyError`. Switch `poll_job` to exponential backoff.

---

## How to use this file

- **Add a new item** when an architecture pass surfaces design-level debt. Score it on the same I/R/E framework. Use the next free `A`-prefixed ID. Don't renumber existing IDs.
- **Close an item** by deleting its row from the backlog table and moving its detail block to a `## Closed` section at the bottom (with the closing PR number). Keep the audit trail.
- **Reference an item from chat** by its stable ID (e.g. *"working on A2 today"*).
- **Re-triage cadence:** after every `/architecture` review (annually-ish), or whenever the codebase doubles in tool count.
- **Don't merge with tech-debt.md.** That ledger is tactical and high-frequency; this one is strategic and low-frequency. Mixing the two makes both worse — tech-debt.md's priority list would be permanently dominated by 1-week strategic items, and this document would be impossible to scan.
