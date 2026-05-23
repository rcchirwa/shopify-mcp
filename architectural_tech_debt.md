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
| 1 | A4 | Stdlib `logging` adoption (per-module loggers, JSON output) — *audit-log rotation done (PR #69)* | Infrastructure | 3 | 3 | 3 | **18** |
| 2 | A3 | Pagination helper for list reads | Code | 2 | 3 | 3 | **15** |
| 3 | A2 | `write_gate()` helper collapsing preview/confirm/error/audit boilerplate — *helper + 7 tools migrated on branch `claude/magical-northcutt-d3977f`* | Code | 4 | 2 | 4 | **12** |
| 4 | A5 | `shopify/` subpackage extraction (`queries/` + `operations/`) with GraphQL fragments | Architecture | 2 | 1 | 2 | **12** |
| 5 | A6 | HTTP client unification (single wrapper for `gql` + `requests`) — *links to N4 in TECH_DEBT.md* | Architecture | 2 | 2 | 3 | **12** |
| 6 | A8 | Metadata `TTLCache` for locations / channels / shop info | Code | 2 | 2 | 4 | **8** |
| 7 | A10 | Committed `uv.lock` for CI reproducibility | Dependency | 1 | 1 | 5 | **2** |

**Categories not represented in current backlog:** Test debt, Documentation debt. The 2026-04-25 review didn't probe these areas in depth — coverage is at 100% and TECH_DEBT.md plus README cover most documentation needs. Re-evaluate during the next architecture pass.

---

## Items

### A2 — `write_gate()` helper

- **Category:** Code
- **Status:** partially done. Helper + 7 tools migrated on branch `claude/magical-northcutt-d3977f` (pending PR). Remaining: 11 standard-pattern tools that could adopt the helper incrementally; 8 complex tools (job-polling, per-item isolation, multi-stage mutations, custom error formatters) intentionally excluded.
- **Cross-reference:** TECH_DEBT.md item **#7** (closed by [#33](https://github.com/rcchirwa/shopify-mcp/pull/33)) collapsed only the hint-string duplication via `with_confirm_hint`. A2 wraps the wider write-tool flow (execute → error check → `log_write`) that #7 left untouched.
- **Impact (4):** every write tool benefits; future tools become 10–20 lines instead of 60–100.
- **Risk (2):** boilerplate duplication is the largest source of subtle drift between tools. A typo silently skips `log_write` or the confirm gate. The helper makes that omission structurally impossible in tools that adopt it.
- **Effort (4):** reduced from 3 — helper is shipped; remaining is mechanical per-tool migration with test verification.
- **Design choice:** helper function, not a decorator. A `@write_tool` decorator that fully owns the flow would require tool bodies to return structured data (preview + execute callable + log description) rather than `str` — that's a bigger API change. The `write_gate()` helper called at the return site achieves the same drift-prevention with zero framework magic; the name at the call site is self-documenting.
- **Shipped this session (branch `claude/magical-northcutt-d3977f`):**
  - `tools/_write_tool.py` — `write_gate()` helper centralising confirm gate, `format_user_errors` check, `log_write`, and done-string return. Accepts `done_text` for tools whose done string differs from preview, and `log_description: str | Callable[[], str]` so non-trivial descriptions aren't computed on the preview path.
  - `test_write_tool_offline.py` — 8 tests covering preview path, default done text, custom `done_text`, callable `log_description` (preview vs confirm, suppression on userErrors), userErrors short-circuit, custom `error_key`, `TransientShopifyError` propagation.
  - `conftest.py` — session-wide autouse fixture patching `_write_tool.log_write` so migrated tools don't pollute the audit log during tests.
  - Migrated tools: `products.update_product_title`, `update_product_description`, `update_product_seo`, `update_product_tags`, `update_product_status`; `collections.update_collection`; `inventory.update_inventory`.
- **Remaining standard-pattern tools** (could adopt the helper):
  - `publications.{publish,unpublish,set}_product_publications` — currently use `extract_user_errors` with custom field-index mapping; would need a partial migration or a richer helper variant.
  - `webhooks.{register,delete}_webhook` — done string depends on mutation result (`numeric_id`, `deletedWebhookSubscriptionId`); would benefit from a `done_text` callable variant.
  - `catalog_hygiene.{update_product_category,update_product_vendor,update_product_type,update_product_pricing}` — use `_format_payload()` JSON-tail format; would need a payload-aware variant.
- **Intentionally NOT migrated** (control flow incompatible with the helper):
  - `products.update_variant_inventory_policy` — custom dotted-field-path error formatter
  - `collections.{add,remove}_product_to_collection` — async job polling via `poll_job()`
  - `inventory.{update_variant_inventory_tracking,update_variant_inventory_quantity}` — per-variant try/except isolation
  - `discounts.create_discount_code` — two-stage mutation with `priceRuleUserErrors` custom key
  - `catalog_hygiene.{set,delete}_product_metafields`, `update_variant_image_binding`, `update_product_options` — multi-step orchestration, JSON-tail format
- **Business justification:** write surface is the highest-risk part of the server (irreversible Shopify mutations). Centralising the safety scaffolding is worth more than just LOC reduction.

### A3 — Pagination helper for list reads

- **Category:** Code
- **Impact (2):** prevents silent truncation on stores with >50 variants per product or >100 media per product.
- **Risk (3):** today, [tools/inventory.py](tools/inventory.py) caps at `_VARIANTS_PAGE_CAP = 50` and [tools/media/_constants.py](tools/media/_constants.py) at `_MEDIA_PAGE_CAP = 100` with no auto-continuation. A product with 100 variants returns the first 50 and the user has no way to know they got truncated.
- **Effort (3):** ~half a day. New helper in `shopify_client.py`; opt-in adoption per tool.
- **Plan:** `paginate(query, variables, page_size, max_pages=10)` walks `pageInfo.hasNextPage` / `endCursor`. Hard-cap on `max_pages` to prevent runaway calls. Tools that risk the cap migrate; tools that genuinely never approach it stay as-is.
- **Business justification:** silent data truncation in a tool that mutates Shopify state is the worst possible failure mode — user thinks they updated all variants, only the first 50 changed.

### A4 — Stdlib `logging` adoption

- **Category:** Infrastructure
- **Status:** partially done. Audit-log `RotatingFileHandler` (10 MB × 5 files) shipped in PR #69. Remaining: per-module `logging.getLogger(__name__)`, `LOG_LEVEL` / `LOG_FORMAT` env vars, JSON output.
- **Impact (3):** transformative the day a tool starts misbehaving in a user's session. No module outside `tools/_log.py` imports `logging` today — debugging requires adding ad-hoc prints.
- **Risk (3):** read tools leave no trace; every contributor reinvents logging; `LOG_LEVEL` can't be tuned at runtime.
- **Effort (3):** ~3 hours remaining. `logging.getLogger(__name__)` per module; `LOG_LEVEL` and `LOG_FORMAT` env vars; configure JSON output via `python-json-logger` when `LOG_FORMAT=json`. (Rotation done; effort adjusted from original 4.)
- **Plan:** log every `client.execute()` at DEBUG with redacted variables; errors at WARNING; startup at INFO. Defer OpenTelemetry — overkill for a single-process MCP server today.
- **Business justification:** observability you don't need until you do, then you need it badly. Cheap to add up-front; expensive to retrofit during an incident.

### A5 — `shopify/` subpackage extraction

- **Category:** Architecture
- **Impact (2):** unblocks query/operation reuse, separates business logic from MCP-tool surface, makes operations testable from non-MCP entry points (CLI, scripts).
- **Risk (1):** no active pain at 8 domains; risk grows with each new domain added without restructuring.
- **Effort (2):** ~1 day. Tool registration stays put; only business logic moves.
- **Plan:** three thin layers — `shopify/queries/` (GraphQL strings, grouped by resource, reusable via fragments), `shopify/operations/` (typed wrappers like `update_product_title(client, id, title) -> dict`), and `tools/` (param coercion, preview/confirm flow, formatting). Pair with GraphQL fragment extraction so `GET_PRODUCT_BY_ID` and `GET_PRODUCT_BY_HANDLE` share their selection set.
- **Business justification:** worth doing **before** the codebase grows past ~12 domains, not after. Mechanical restructuring is cheap at small scale and exponentially more expensive once dependencies have accumulated.

### A6 — HTTP client unification

- **Category:** Architecture
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

### Phase 1 — Foundational (~1 day remaining)

A1 shipped out of order (PR #70, as security fix M5). A4's rotation shipped (PR #69, security fix M4). A7's `Settings` class landed on branch `claude/relaxed-hertz-8f050d`. Remaining work:

| Day | Item | Why |
|-----|------|-----|
| 1 | **A4** Logging (remainder) | Per-module loggers + env vars. `log_level` and `log_format` fields already exist on `Settings` — wiring is all that's left. Rotation already done. |

~~**A1** — closed, PR #70~~
~~**A7** — closed, branch `claude/relaxed-hertz-8f050d`~~

### Phase 2 — Tool surface (~1 day remaining)

A2's helper and proof-of-pattern migration (7 tools) shipped on branch `claude/magical-northcutt-d3977f` this session. Remaining:

| Day | Item | Why |
|-----|------|-----|
| 0.5 | **A2 (remainder)** Migrate the remaining standard-pattern tools — publications, webhooks (needs `done_text` callable variant), catalog_hygiene standard tools (needs JSON-tail variant) | Pattern is proven; remaining is mechanical per-tool work with test verification. |
| 0.5 | **A3** Pagination helper | Quick win; prevents silent truncation. |

### Phase 3 — Restructure (do before reaching ~12 domains or starting multi-store work, ~3 days)

| Day | Item | Why |
|-----|------|-----|
| 1–2 | **A5** `shopify/` subpackage | Restructure before the codebase grows past the size where mechanical reshuffling is cheap. |
| 2–3 | **A6** HTTP unification | Pairs naturally with A5; closes TECH_DEBT.md N4. |

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
