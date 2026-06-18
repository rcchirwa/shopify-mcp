# Architectural Tech Debt

Strategic, design-level technical debt for `shopify-mcp`. Sibling to [TECH_DEBT.md](TECH_DEBT.md), which tracks tactical / code-level items.

**Scope:** patterns and structures that constrain future scaling, maintainability, or extensibility — debt that won't surface as a single failing test but will compound as the codebase grows.

**Format:** static ledger, updated when the architecture genuinely shifts (not after every PR). The journal-style triage workflow lives in TECH_DEBT.md; this document captures the long-arc design concerns that don't move on a daily cadence.

**Scoring:** `Priority = (Impact + Risk) × (6 − Effort)`, each axis 1–5, effort inverted. Same framework as TECH_DEBT.md so items can be triaged together when needed. **Ties broken by Impact descending, then by ID ascending.**

**Source:** initial inventory derived from the 2026-04-25 architecture review (10 evaluation areas across organization, tool surface, error handling, auth, caching, rate limiting, reuse, deps, config, observability).

---

## Backlog (priority-ordered)

| Rank | ID | Item | Category | I | R | E | Score |
|------|----|------|----------|---|---|---|-------|
| 1 | A3 | Pagination helper for list reads — *helper shipped + read-path adoption Story 10.16* | Code | 2 | 3 | 3 | **15** |
| 2 | A2 | `write_gate()` helper collapsing preview/confirm/error/audit boilerplate — *closed Story 10.22; 9 tools migrated; remaining tools triaged and deliberately excluded* | Code | 4 | 2 | 4 | **12** |
| 3 | A5 | `shopify/` subpackage extraction (`queries/` + `operations/`) with GraphQL fragments | Architecture | 2 | 1 | 2 | **12** |
| 4 | A6 | HTTP client unification (single wrapper for `gql` + `requests`) — *closed: policy half N4/Story 10.21, transport half Story 10.24 (`client.fetch_bytes()` + shared `_with_retry`)* | Architecture | 2 | 2 | 3 | **12** |
| 5 | A8 | Metadata `TTLCache` for locations / channels / shop info | Code | 2 | 2 | 4 | **8** |
| 6 | A10 | Committed `uv.lock` for CI reproducibility | Dependency | 1 | 1 | 5 | **2** |

**Categories not represented in current backlog:** Test debt, Documentation debt. The 2026-04-25 review didn't probe these areas in depth — coverage is at 100% and TECH_DEBT.md plus README cover most documentation needs. Re-evaluate during the next architecture pass.

---

## Items

### A2 — `write_gate()` helper

- **Category:** Code
- **Status:** ✅ **closed — Story 10.22.** All standard-pattern tools have been migrated or deliberately excluded with technical justification. 9 tools now use `write_gate()`.
- **Cross-reference:** TECH_DEBT.md item **#7** (closed by [#33](https://github.com/rcchirwa/shopify-mcp/pull/33)) collapsed only the hint-string duplication via `with_confirm_hint`. A2 wraps the wider write-tool flow (execute → error check → `log_write`) that #7 left untouched.
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
- **Status:** helper shipped — `ShopifyClient.paginate()` at [shopify_client.py:224](shopify_client.py:224), tested in [test_paginate_offline.py](test_paginate_offline.py), mirrored in `_testing/fake_client.py`. Every cleanly-paginable single-object read has adopted it (inventory + media under Story 10.6; orders, products, publications under Story 10.16). Remaining gaps are the two structural exceptions below.
- **Impact (2):** prevents silent truncation on stores with >50 variants per product or >100 media per product.
- **Risk (3):** the helper-adopted read paths now auto-continue across pages. The residual risk is the two connections `paginate()` structurally cannot walk — both documented, note-only items in TECH_DEBT.md (`A3-orders-lineitems-cap`, `A3-option-echo-cap`): `GET_ORDERS.nodes.lineItems` (a connection nested inside a list, so `get_orders` still caps per order) and the `UPDATE_PRODUCT_OPTION` mutation-response echo (mitigated by a pre-write at-cap warning).
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
- **Status (Story 10.23 — in progress):** the `shopify/` package and the three-layer
  structure are **established**, with the `products` domain migrated as the pilot:
  - `shopify/queries/products.py` holds all product GraphQL strings; shared
    fragments `ProductCoreFields` / `ProductFullFields` dedup the by-id and
    by-handle selection sets.
  - `shopify/operations/products.py` holds typed wrappers that take a duck-typed
    `GraphQLClient` (`shopify/_client.py`) and are callable without FastMCP;
    `tools/products.py` now delegates to them and keeps only coercion +
    preview/confirm + formatting.
  - The one-way rule (`shopify/` never imports `tools/`) is enforced by
    `test_shopify_layering_offline.py`.
  - **Q3-helper decision:** the GID helpers moved to `shopify/_ids.py` (the
    operations layer needs `to_gid` and must not import `tools/`); `tools/_gid.py`
    is now a thin re-export shim so existing `from tools._gid import ...` call
    sites are unchanged. `tools/_response.py` **stays in `tools/`** — its helpers
    (`with_confirm_hint`, `extract_user_errors`) are preview/response-formatting
    concerns used by the tool layer; revisit if a `shopify` operation ever needs
    `extract_user_errors`.
  - **Remaining:** migrate `catalog_hygiene`, then `collections`, `discounts`,
    `inventory`, `orders`, `publications`, `webhooks` — one domain per PR. A5
    closes once all domains are migrated. Effort estimate revised: the full
    sweep is larger than ~1 day (catalog_hygiene alone is ~4,600 lines / ~94
    GraphQL blocks), hence the incremental approach.

### A6 — HTTP client unification

- **Category:** Architecture
- **Status:** ✅ **closed — Story 10.24.** Both halves done. The **policy** half (shared User-Agent + config-driven timeouts across both stacks) closed under **N4 / Story 10.21 (PR #87)**. This story closed the **transport** half: `ShopifyClient.fetch_bytes(url, *, max_size, allow_redirects=False)` now wraps the image-download GET with the SSRF guard, shared headers, the `Settings.download_timeout_s` timeout, a streaming size cap, redirect refusal, and retry on retryable statuses (429/5xx). The gql retry loop and `fetch_bytes` now share **one** backoff implementation (`ShopifyClient._with_retry`), so there is no duplicated retry logic. The staged-upload PUT deliberately stays a single-shot `requests.put` (non-idempotent large signed upload — see the grooming decision on the Story 10.24 card and the code comment in `_upload_bytes_to_target`); it still shares the HTTP *policy* via `default_headers`, only automatic retry is excluded.
- **Cross-reference:** TECH_DEBT.md item **N4** (watch). N4's trigger is "a second tool starts using `requests` directly." A6 is the architectural framing of the same concern.
- **Impact (2):** one retry policy, one timeout config, one User-Agent. Foundation for A1's retry/backoff to apply uniformly.
- **Risk (2):** today [tools/media/_upload.py:18](tools/media/_upload.py:18) uses `requests` directly for image downloads alongside `gql`'s `RequestsHTTPTransport`. Two stacks means two failure modes the user has to learn.
- **Effort (3):** ~half a day. `client.fetch_bytes(url, max_size=...)` wrapper exposed off `ShopifyClient`.
- **Plan:** unify under a single client wrapper exposing both GraphQL execution and arbitrary HTTP fetches. Image download in `tools/media/_upload.py` becomes `client.fetch_bytes(url, max_size=...)`. Pairs naturally with A1 (shared retry policy across both).
- **Business justification:** rolls together with A1 — once the throttle-aware policy exists, having two HTTP stacks means only half of calls benefit.

### A8 — Metadata `TTLCache`

- **Category:** Code
- **Impact (2):** reduces latency and Shopify quota burn for stable metadata (locations, publication channels, metafield definitions, shop info).
- **Risk (2):** real but not urgent at current call volumes. The dead `channel_cache` in [tools/publications.py](tools/publications.py) shows the intent existed but the implementation slipped — every MCP call re-resolves channels from scratch.
- **Effort (4):** ~half a day. `cachetools.TTLCache` attached to `ShopifyClient`.
- **Plan:** `ShopifyMetadataCache` with TTL'd entries — shop info (24h), locations (1h), publication channels (10m), metafield definitions (10m). Configurable via Settings (A7). Invalidate on writes that mutate the cached resource.
- **Business justification:** pays off as soon as automation increases. Until then, defer.

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
| 1–2 | **A5** `shopify/` subpackage *(in progress — Story 10.23 landed the structure + `products` pilot; remaining domains migrate one per PR)* | Restructure before the codebase grows past the size where mechanical reshuffling is cheap. |
| 2–3 | ~~**A6** HTTP unification~~ *(closed — Story 10.24; `client.fetch_bytes()` + shared `_with_retry`. Policy half was N4/Story 10.21.)* | Pairs naturally with A5; closes TECH_DEBT.md N4. |

### Backlog (don't pre-refactor)

| Item | Trigger |
|------|---------|
| **A8** Caching | Call volume rises, or first real Shopify quota miss. |
| **A10** Lockfile | First CI-vs-dev divergence caused by a floated dep. |

---

## Closed

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
- **Don't merge with TECH_DEBT.md.** That ledger is tactical and high-frequency; this one is strategic and low-frequency. Mixing the two makes both worse — TECH_DEBT.md's priority list would be permanently dominated by 1-week strategic items, and this document would be impossible to scan.
