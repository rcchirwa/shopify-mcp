# Tech Debt Ledger

Living record of the technical-debt triage for `shopify-mcp`. Newest entry first. Future Claude sessions: read the latest dated section and work forward — older sections are kept for trend context only.

Scoring: `Priority = (Impact + Risk) × (6 − Effort)`, each axis 1–5, effort inverted.

**Last full audit:** 2026-04-24. **Last follow-up:** 2026-05-24.

---

## 2026-05-26 — Story 10.9 follow-up (resolver orphan + collections warning)

Two items from the 2026-05-21 backlog closed in a single PR.

### Closed

| # | Item | How it closed |
|---|------|---------------|
| T-9.6-resolver-orphan | ~~`resolve_variant_ids_to_gids` / `resolve_variant_id_to_gid` have no callers~~ | Deleted both functions and `GET_PRODUCT_VARIANTS_FOR_RESOLVE` from `tools/_resolvers.py`. Also removed now-unused `ShopifyClient` import from the module. Deleted 19 orphaned test cases from `test_resolvers_offline.py` (plus the stale `FakeClient` import). `_validate_variant_id` and `_classify_no_fetch` were confirmed still live (used by `resolve_variant_ids_with_variants`) and kept. CI clean: 924 tests, 100% coverage. |
| SEC-M2-collection-seo | ~~`update_collection` warning format inconsistent with `update_product_description`~~ | Replaced the inline comma-joined `"\n  ⚠ DANGEROUS HTML DETECTED: ..."` suffix in `tools/collections.py` with the same bullet-point block used by `tools/products.py`: `"\n\n⚠ DANGEROUS HTML DETECTED in new description:\n" + "\n".join(f"  • {p!r}" ...)`. Existing test assertions (`assert "⚠ DANGEROUS HTML DETECTED" in out`, `assert "'<script'" in out`) survived unchanged — they use substring checks that hold under both formats. |

### Current active backlog (after Story 10.9)

| Rank | Item | Score | Status |
|------|------|-------|--------|
| 1 | SEC-M2-sanitizer — advisory blocklist → proper HTML sanitizer | 16 | active (needs product sign-off) |
| 2 | N4 — two HTTP stacks, no shared policy | 9 | watch |
| 3 | T-9.5-resolver-fanout — 4 near-twin `_resolve_product_id_*` helpers | 8 | watch |
| 4 | T-9.6-media-cap — `media(first: 100)` silent truncation | 6 | watch |
| 5 | T-9.5-variants-cap — `variants(first: 50)` post-write snapshot cap | 6 | partial |
| 6 | O1 — mypy permissive on test files (pre-existing) | — | watch |
| 7 | Q4 — `format_user_errors_joined()` single caller | 4 | note |
| 8 | Q5 — `dict[str, Any]` baseline for GraphQL payloads | 4 | watch |
| 9 | Q6 — `from_gid` type widening note | 4 | note |

---

## 2026-05-21 — Q3 follow-up (shopify_client.py split)

Item Q3 from the 2026-04-24 audit was implemented this session. A `/engineering:code-review` + `/security-review` pass surfaced three suggestions, all applied before closing.

### Closed

| # | Item | How it closed |
|---|------|---------------|
| Q3 | ~~`shopify_client.py` becoming a utility grab-bag~~ | Extracted `to_gid`/`from_gid` → `tools/_gid.py`; `with_confirm_hint`, `extract_user_errors`, `format_user_errors_joined`, `format_user_errors` → `tools/_response.py`. 19 modified files, 2 new modules, 2 new test files (`test_gid_offline.py`, `test_response_offline.py`). `shopify_client.py` trimmed 391 → 310 lines. CI (ruff + mypy + pytest + 100% coverage) clean throughout. Three code-review suggestions also applied: `extract_user_errors` return type widened to `list[dict[str, Any]]`; inline `to_gid` import hoisted to module top in `tools/orders.py`; defensive `None: None` fallback documented in `format_user_errors_joined` docstring. |

### New items found

| # | Item | Where | Score |
|---|------|-------|-------|
| Q6 | **`from_gid` type widened to `str \| None`** — the original `shopify_client.py` signature declared `gid: str` but callers pass `None` from `obj.get("id")` patterns; the body handled it silently. The split corrected this to `str \| None`. Downstream callers that now type-check cleanly may have been masking `None`-propagation bugs. No action needed — note for the next mypy tightening pass (O1). | `tools/_gid.py` | 4 (note) |

### Current active backlog (after Q3 closes)

| Rank | Item | Score | Status |
|------|------|-------|--------|
| 1 | SEC-M2-sanitizer — advisory blocklist → proper HTML sanitizer | 16 | active |
| 2 | N4 — two HTTP stacks, no shared policy | 9 | watch |
| 3 | T-9.5-resolver-fanout — 4 near-twin `_resolve_product_id_*` helpers | 8 | watch |
| 4 | T-9.6-media-cap — `media(first: 100)` silent truncation | 6 | watch |
| 5 | T-9.5-variants-cap — `variants(first: 50)` post-write snapshot cap | 6 | partial |
| 6 | O1 — mypy permissive on test files (pre-existing) | — | watch |
| 7 | T-9.6-resolver-orphan — `resolve_variant_ids_to_gids` has no callers | 4 | confirmed |
| 8 | SEC-M2-collection-seo — collections warning format inconsistent with products | 4 | note |
| 9 | Q4 — `format_user_errors_joined()` single caller | 4 | note |
| 10 | Q5 — `dict[str, Any]` baseline for GraphQL payloads | 4 | watch |
| 11 | Q6 — `from_gid` type widening note | 4 | note |

(T-9.6-unknown-variant closed 2026-05-24 — see 2026-05-12 Story 9.6 section follow-up.)

Q3 closes; active backlog top item is now SEC-M2-sanitizer (score 16).

---

## 2026-05-18 — M2 security review follow-ups (PR #65)

Items surfaced by the mcp-server-security-review.md (2026-05-18) and the subsequent `/engineering:code-review` + `/security-review` pass on the M2 implementation PR. The blocklist-based advisory warning shipped in PR #65 is a solid first layer; the items below track its known gaps for future hardening.

### Active

| # | Item | Where | Score |
|---|------|-------|-------|
| SEC-M2-sanitizer | **Advisory blocklist should migrate to a proper HTML sanitizer** — `dangerous_html_patterns()` in `tools/_filters.py` detects a curated set of dangerous substrings and regex patterns (`on\w+=`, `<script`, `javascript:`, etc.) and emits an advisory warning in the preview. This is defence-in-depth, not a true sanitizer. A sufficiently creative payload (CSS injection via `<style>`, attribute injection in unexpected HTML contexts, Unicode lookalike characters) can slip through without a warning and be written to `descriptionHtml` / `seo.*` without operator friction. **Recommended path:** replace or wrap `dangerous_html_patterns()` with `nh3` (Rust-backed port of `ammonia`) or `bleach` (Python) in an allow-list mode: strip everything except an explicitly allowed tag+attribute set before calling Shopify. This changes the written content semantics (stripped tags), so it requires product sign-off before landing. Trigger: any Epic that touches bulk description writes or introduces a new HTML-bearing field. | `tools/_filters.py`, `tools/products.py`, `tools/collections.py` | 16 |
| SEC-M2-collection-seo | **`update_collection` description preview warning uses inline suffix format inconsistent with `update_product_description`** — the warning for collections appears as `\n  ⚠ DANGEROUS HTML DETECTED: ...` appended to the preview line, while the product description warning is a distinct block with bullet-pointed pattern names. Minor UX inconsistency; low priority but should be unified when either path is next touched. | `tools/collections.py:update_collection`, `tools/products.py:update_product_description` | 4 (note) |

### Closed

| # | Item | How it closed |
|---|------|---------------|
| SEC-M2-truncation | ~~`update_product_description` preview truncated new content to 120 chars — operator couldn't see full payload before confirming~~ | PR #65 — preview now shows full `new_description` under `New (full):` label. |
| SEC-M2-collection-placeholder | ~~`update_collection` showed `(new description provided)` placeholder — operator saw nothing of the actual new content~~ | PR #65 — old excerpt + full new description now shown. |
| SEC-M2-redirect | ~~SSRF redirect bypass (`allow_redirects=True` default in `_download_image`)~~ | PR #64 (HIGH finding H1, 2026-05-18). |
| SEC-M2-onstar-fixed | ~~Fixed `onerror=` / `onload=` as discrete strings — missed `onload =` (space before `=`) and all other `on*=` handlers (`onclick=`, `ontoggle=`, `onfocus=`, etc.)~~ | PR #65 — switched to `re.compile(r"\bon\w+\s*=", re.IGNORECASE)` regex; all event-handler variants now caught. |
| SEC-M2-schemes | ~~`vbscript:`, `data:text/html`, `</title>` not in danger pattern list~~ | PR #65 — added to `_DANGEROUS_HTML_EXACT` tuple. |

---

## 2026-05-12 — Story 9.5 follow-ups

Items surfaced during implementation of Story 9.5 (`update_product_options`). Not yet triaged against the active priority list — score columns are estimates. Pending next full re-audit.

### Active

| # | Item | Where | Score |
|---|------|-------|-------|
| T-9.5-variants-cap | **`variants(first: 50)` silent truncation in the post-write snapshot** — both `GET_PRODUCT_OPTIONS` and the `productOptionUpdate` mutation echo cap the returned variants slice at 50. A product with more than 50 variants would see the JSON tail's `product.variants` truncated without warning. Matches the pattern called out by T-9.6-media-cap. Fix path: paginate or emit a `hasNextPage` warning. Trigger: a real product hits > 50 variants. | `tools/catalog_hygiene.py:GET_PRODUCT_OPTIONS`, `:UPDATE_PRODUCT_OPTION` | 6 (watch) |
| T-9.5-resolver-fanout | **Per-story product-id resolvers proliferating** — `_resolve_product_gid` (9.1), `_resolve_product_id` (9.2), `_resolve_product_id_for_type` (9.4), and now `_resolve_product_id_for_options` (9.5) are near-twins differing only in the GraphQL query. Story 9.1's helper returns `(gid, error_str)` while 9.2/9.4/9.5 return `(gid, product_snapshot)` — two shapes but the same numeric/GID/handle dispatch logic. With Epic 9 effectively closed (9.1-9.7 all shipped), the trigger is now any Epic-10+ catalog-hygiene tool that needs a product read — that will fire on Story 10.1 unless that tool reuses one of the existing helpers. Fix path: a single `_resolve_product_id_with_query(client, product_id, query_id, query_by_handle)` that takes the two queries as args, or hoist a shared helper into a `tools/_product_resolvers.py`. Don't refactor in 9.5 — wait until the next net-new helper would land, then refactor it + the four existing twins in one PR. | `tools/catalog_hygiene.py:_resolve_product_gid, _resolve_product_id, _resolve_product_id_for_type, _resolve_product_id_for_options` | 8 (watch) |

### Closed

None — no Story 9.5 debt closed during this story.

T-9.5-variants-cap mirrors T-9.6-media-cap (both first-N pagination caps in the same module) and may close as a single fix. T-9.5-resolver-fanout is a structural watch item, not actionable until the 4th twin lands.

---

## 2026-05-12 — Story 9.6 follow-ups (updated 2026-05-12 post-PR #52)

Items surfaced during code review of Story 9.6 (`update_variant_image_binding`) and the PR #52 follow-up review. Not yet triaged against the active priority list — score columns are estimates. Pending next full re-audit. The in-code module docstring at `tools/catalog_hygiene.py` references the active IDs.

### Active

| # | Item | Where | Score |
|---|------|-------|-------|
| ~~T-9.6-unknown-variant~~ | **Closed 2026-05-24** — see Follow-up below. | — | — |
| T-9.6-media-cap | **`media(first: 100)` silent truncation** — `update_variant_image_binding` validates input mediaIds against the first 100 product-media nodes only; a valid media GID past that window would be falsely rejected as not-on-product. Matches the existing cap in `tools/media/_graphql.py:GET_PRODUCT_MEDIA`. Fix path: paginate, or surface a "first-100-only" warning when `pageInfo.hasNextPage` is true. Trigger: a real product hits >100 media. | `tools/catalog_hygiene.py`, `tools/media/_graphql.py` | 6 (watch) |
| T-9.6-resolver-orphan | **Story 9.0's `resolve_variant_ids_to_gids` / `resolve_variant_id_to_gid` now have no production callers** — confirmed via `grep -rn "resolve_variant_ids_to_gids" tools/ validators/ shopify_client.py` (no matches outside `tools/_resolvers.py` itself). Story 9.3 and Story 9.6 both use `resolve_variant_ids_with_variants`. The fetching resolver is exercised only by `test_resolvers_offline.py`. Don't remove in #52 — it's a public helper and future stories may want the fetch-on-its-own variant. But flag for a quick cleanup PR if no caller materializes by Story 9.7. | `tools/_resolvers.py` | 4 (note) |
| T-9.6-error-msg-shift | **Product-not-found message changed in PR #52** — pre-swap, SKU lookup against a missing product surfaced the resolver's `ValueError("Product not found: <gid>")`; post-swap, the combined query's null `product` produces `"No product found with id <product_id>"` (echoes the user's raw input rather than the resolved GID). Functionally equivalent and probably more helpful for debugging, but a behavioral shift to track in case any downstream string-matching consumer relied on the old form. No action expected — note-only for trend-watch. | `tools/catalog_hygiene.py:update_variant_image_binding` | 2 (note) |

### Closed

| # | Item | How it closed |
|---|------|---------------|
| T-9.6-rt | ~~Worst-case 3 round-trips when SKUs are supplied~~ | [#52](https://github.com/rcchirwa/shopify-mcp/pull/52) — switched to Story 9.3's `resolve_variant_ids_with_variants` enabler. Variant resolution now runs in-memory against the combined query's `variants.nodes`, collapsing SKU-input round-trips from 3 to 2 (combined + mutation). |
| T-9.6-handle | ~~Product-ID handle resolution missing~~ | PR #71 — wired `update_variant_image_binding` through `_resolve_product_gid`; added wrong-GID-type guard to that resolver to preserve zero-network-call invariant for non-Product GID inputs. |
| T-9.6-unknown-variant | ~~Unknown numeric/GID variantIds slip past resolution into the mutation~~ | Follow-up 2026-05-24 — added Step 3b in `update_variant_image_binding` (`tools/catalog_hygiene.py`). Mirrors the existing fail-fast check at Step 4 (media-GID rejection): walks `resolved_variant_gids` against the pre-fetched `variant_media_map`, dedupes, and emits `Error: variant GIDs not on product <product_id>: <gid>, ...` before either `productVariantDetachMedia` or `productVariantAppendMedia` runs. Four new offline tests added (`test_s96_unknown_numeric_variant_id_rejected`, `test_s96_unknown_variant_gid_rejected`, `test_s96_multiple_unknown_variants_listed_once_each`, `test_s96_mixed_valid_and_unknown_variant_rejected_wholesale`). `/engineering:code-review` + `/security-review` pass clean (security review: no findings — change is itself a hardening). Full CI clean (ruff + mypy + 929 tests + 100% coverage). |

No active item lands above the priority list. T-9.6-unknown-variant is the highest-scored remaining item; pre-existing (predates #52) and deferred-pending-trigger.

### Follow-up (added 2026-05-24, T-9.6-unknown-variant)

T-9.6-unknown-variant closed. Two-agent review pass (`/engineering:code-review` + `/security-review`) ran on the diff before commit:

1. **Code-review verdict:** Approve. One actionable suggestion — add a *mixed-batch* test (one valid + one unknown variant) to harden against a future refactor that accidentally partitions valid/invalid and proceeds with the valid subset. Applied: `test_s96_mixed_valid_and_unknown_variant_rejected_wholesale` asserts the whole batch errors and zero mutations run. Other notes were cosmetic (error-message rendering convention asymmetry vs. the adjacent media-GID check; cross-reference anchor in this ledger; a possible future-coupling with T-9.5-resolver-fanout where the guard could move into the resolver itself) — not acted on.

2. **Security-review verdict:** No findings. The change is itself a security/correctness hardening — it converts an authorization-adjacent fail-late (a cross-product variant GID flowing into a Shopify write mutation, rejected only server-side) into a clean tool-side rejection before any mutation runs. No new injection, deserialization, SSRF, or data-exposure surface introduced.

3. **Forward note for T-9.5-resolver-fanout:** the new guard exists *because* `resolve_variant_ids_with_variants` short-circuits numeric/GID inputs without product-membership validation. If/when the four `_resolve_product_id_*` twins are consolidated, consider also tightening `resolve_variant_ids_with_variants` to perform the membership check itself — at which point Step 3b in `update_variant_image_binding` (and the equivalent block in `update_product_pricing`) becomes redundant and can collapse into the resolver. Not in scope today.

### Follow-up (added 2026-05-21, PR #71)

T-9.6-handle closed. Beyond the handle-wiring itself, PR #71 made three additional improvements worth tracking:

1. **Wrong-GID-type guard added to `_resolve_product_gid`** — inputs like `gid://shopify/Order/1` now short-circuit before the handle path with no network call. This benefit extends to all callers: `update_product_category` (9.1), `update_product_vendor` (9.2 via its own twin), `update_product_type` (9.4 via its own twin), `update_product_options` (9.5 via its own twin), and now `update_variant_image_binding` (9.6). The four near-twin resolvers noted in T-9.5-resolver-fanout each have identical `startswith("gid://")` entry points — the guard should be added to the other three twins too, either now or as part of the T-9.5-resolver-fanout consolidation.

2. **`GET_PRODUCT_BY_HANDLE_MIN` trimmed to `id` only** — `title` and `category { id fullName name }` were fetched but never read by `_resolve_product_gid`. Query is now minimal. The other three "by-handle" queries (`GET_PRODUCT_VENDOR_BY_HANDLE`, `GET_PRODUCT_TYPE_BY_HANDLE`, `GET_PRODUCT_OPTIONS_BY_HANDLE`) fetch only what their respective tools need and are not affected.

3. **`_resolve_product_gid` now shared across 9.1 and 9.6** — previously only `update_product_category` used it; `update_variant_image_binding` is the second caller. The T-9.5-resolver-fanout consolidation (if it lands) will absorb all four twins into one; the 9.6 call site will naturally migrate at that point.

---

## 2026-04-24 — Re-triage

Baseline was the 2026-04-23 audit. 10 commits / 5 PRs merged in the 24 hours since. The day's theme: every actionable item from the prior audit's phased plan landed. The top-of-stack queue is essentially drained.

### Closed since last triage (2026-04-24)

| # | Item | How it closed |
|---|------|---------------|
| N3 | **SSRF guard scope** (was 24, rank 1) | Hoisted to `tools/_url_safety.py` (50 LOC, exported `_reject_if_private_host`). New `test_url_safety_offline.py` adds 9 dedicated tests. Module docstring now reads "Any new tool that accepts a caller-supplied URL and dereferences it server-side MUST call `_reject_if_private_host` before issuing the request" — discoverability fixed. (`c533d53`) |
| 2 | **`userErrors` extraction (3 flavors)** (was 22, rank 2) | `extract_user_errors(result, mutation_key, *, error_key="userErrors")` and `format_user_errors(..., prefix="Error")` added to `shopify_client.py:206-246`. 79 callsites adopted across the codebase. Three holdouts remain and are documented as intentional divergences in the helper docstring: `products.py:847` (list-path `field`), `inventory.py:325` (per-variant loop in bulk op), `media/_common.py:25` (stage-aware fallback). (`68ce473`) |
| N2 | **Duplicate job-poll timeout constant** (was 20, rank 3) | `JOB_POLL_TIMEOUT_S = 10` now lives in `shopify_client.py:32` as the `poll_job` default; per-module copies deleted. (`2af4d21`) |
| 7 | **Preview/confirm gate (28×)** (was 20, rank 4) | `with_confirm_hint(preview)` added to `shopify_client.py:57`. The literal `"To apply, call again with confirm=True."` now appears exactly **once** in the codebase. (`2da3199`) |
| 4 | **SEO constants misplaced** (was 20, rank 5) | New `validators/seo.py` (10 LOC) holds `SEO_TITLE_MAX_CHARS` and `SEO_DESCRIPTION_MAX_CHARS`. (`fcc7933`) |
| N1 | **`tools/media.py` 917 lines** (was 15, rank 6) | Split into the `tools/media/` package: `_list.py` (55), `_upload.py` (446), `_reorder.py` (128), `_update.py` (68), `_delete.py` (106), plus `_common.py`, `_constants.py`, `_graphql.py`. `__init__.py` keeps a compat facade with `# noqa: F401` re-exports. (`5b1cad0`, `51aa20e`) |
| 3 | **No lint/type-check in CI** (was 15, rank 7) | New `lint` job in `.github/workflows/test.yml:39-53` runs `ruff check`, `ruff format --check`, and `mypy` — as a separate job from offline-tests so failures show as distinct red checks. Ruff config in `pyproject.toml:51-76` selects `E,W,F,I,UP,B,SIM,C4,RUF`; mypy is permissive baseline with `check_untyped_defs` on. (`607cd2b`) |
| 8 | **`test_webhooks.py` pyproject ignore** (was 15, rank 8) | `pyproject.toml:42` now ignores both `test_shopify_mcp.py` and `test_webhooks.py` from default discovery. (`ac059a7`) |

Also notable: **5 worktrees remain** under `.claude/worktrees/` (down from 8+). N5 is partially addressed.

### Unchanged or worse

| # | Item | Δ | Score |
|---|------|---|-------|
| N4 | **Two HTTP stacks, no shared policy** | unchanged. Still one `requests` caller in `tools/media/_upload.py`. Watch only. | **9** |
| N5 | **Worktree cleanup** | 5 left from 8+. Partial. | **8** |

### New items found

| # | Item | Where | I | R | E | Score |
|---|------|-------|---|---|---|-------|
| O1 | **Mypy is permissive baseline** — pyproject explicitly defers `disallow_untyped_defs` until "signatures are filled in" (`pyproject.toml:80-81`). Type debt is now *measured* but not gated. Practical risk: typos in type-relevant code paths (e.g. `result.get("foo")` returning `None`-then-dict-method) won't be caught until mypy is tightened. | `pyproject.toml:78-89` | 2 | 2 | 3 | **12** |
| O2 | **`tools/media/__init__.py` re-export shim** — keeps `from tools.media import _download_image, GET_PRODUCT_MEDIA, …` working with `# noqa: F401`. Old test/import paths haven't migrated. Removing the shim will mean grep-and-replace in test files; cheaper to do now (small surface) than after more callers accumulate. | `tools/media/__init__.py:14-29` | 2 | 2 | 2 | **16** |
| O3 | **`tools/media/_upload.py` at 446 lines** — biggest file in the new package; holds download + SSRF call + staged-upload PUT + media attach + post-attach polling. The flow is a pipeline; could split into `_download.py` + `_stage_and_attach.py`. Don't pre-refactor; flag for next time anything inside changes. | `tools/media/_upload.py` | 2 | 1 | 3 | **9** |
| O4 | **One inline `msgs = "; ".join(...)` in `inventory.py:325`** — only "non-justified" holdout from item #2 cleanup. The site is inside a per-variant loop in a bulk op and wants the joined message *without* the canonical helper's `"Error: "` prefix. Could be solved with a `format_user_errors_messages_only(...)` variant or by stripping the prefix at the call site. Tiny. | `tools/inventory.py:325` | 1 | 1 | 1 | **10** |

### Current priority list

| Rank | Item | Score | Status |
|------|------|-------|--------|
| 1 | O2 — `media/__init__.py` re-export shim | 16 | new |
| 2 | O1 — Mypy permissive baseline | 12 | new |
| 3 | O4 — Inline `msgs.join` in `inventory.py` | 10 | new |
| 4 | N4 — Two HTTP stacks, no shared policy | 9 | watch |
| 5 | O3 — `media/_upload.py` at 446 lines | 9 | watch |
| 6 | N5 — Worktree cleanup (5 left) | 8 | partial |

No item scores ≥ 20. **The backlog is in the lowest state since this ledger started.**

### Phased plan

**Anytime in the next 2 weeks (~half a day total):**
- **O2** — Migrate the `# noqa: F401` re-exports off. Grep `from tools.media import _download_image|_format_bytes|_upload_bytes_to_target|_render_media_list|_as_product_gid|GET_PRODUCT_MEDIA|GET_MEDIA_STATUS|PRODUCT_CREATE_MEDIA|PRODUCT_DELETE_MEDIA|PRODUCT_REORDER_MEDIA|PRODUCT_UPDATE_MEDIA|STAGED_UPLOADS_CREATE` and update each to its proper submodule path. Then delete the shim block in `tools/media/__init__.py:14-29`. CI will tell you if you missed any.
- **O4** — Either add a `messages_only=True` flag (or a `format_user_errors_joined()` thin wrapper) to `shopify_client.py:226`, or strip the `"Error: "` prefix at the call site. 10 minutes either way.
- **N5** — Prune `.claude/worktrees/{blissful-matsumoto-5f8af7, friendly-chatterjee-862e1c, nervous-golick-0ac12c, vigorous-diffie-7500d6, wonderful-wiles-6ac1a5}` after confirming none have unmerged work.

**Backlog (don't pre-refactor):**
- **O1** — Plan a typing pass: enable `disallow_untyped_defs` per-module starting with `shopify_client.py` and `validators/`, then work outward. Whole-codebase tightening probably 1–2 days. Schedule when no major feature is in flight.
- **O3** — Split `_upload.py` only when its next change makes the size meaningfully worse.
- **N4** — A second `requests` caller will be the trigger.

### Net assessment

Yesterday I called the trajectory "good." Today I'd call it *exceptional* — the entire top-8 priority list closed in one day, and three of the closures (the `userErrors` helpers, the `with_confirm_hint`, the SSRF hoist) made the codebase structurally easier to extend rather than just patching debt. The new findings are all small, all forward-looking, and none compound. If this ledger were a sprint board, we'd be talking about pulling work forward from next quarter.

The one place to apply pressure: **decide on the typing-tightness curve before the codebase grows**. Mypy-permissive-now / strict-later is fine policy but only if "later" has a date.

### Remediation PRs (added 2026-04-24)

Post-audit grouping of the remaining backlog into PR-sized batches. PR numbers are local to this audit — when you reference "PR 2" in a chat, point Claude at this section.

| PR | Item(s) | Files | Estimate | Suggested commit title |
|----|---------|-------|----------|------------------------|
| **PR 1** | O4 — collapse last inline `userErrors` join | `shopify_client.py`, `tools/inventory.py`, `test_inventory_offline.py`, `test_shopify_client_offline.py` | ~15 min | `refactor: collapse last inline userErrors join via format_user_errors variant` |
| **PR 2** | O2 — drop `tools/media/__init__.py` re-export shim | `tools/media/__init__.py`, every test file importing private names from `tools.media` | ~30 min | `refactor: drop tools.media facade re-exports, migrate callers to submodules` |
| **PR 3** | O1 — typing pass (`disallow_untyped_defs = true`) | `pyproject.toml` + annotations across `shopify_client.py`, `validators/`, `tools/` | ~1 day | `chore: enable disallow_untyped_defs and fill remaining annotations` |
| **PR 3a** *(if splitting PR 3)* | O1 — typing pass: core | `pyproject.toml`, `shopify_client.py`, `validators/` | ~2 hr | `chore: tighten mypy on shopify_client and validators` |
| **PR 3b** *(if splitting PR 3)* | O1 — typing pass: tools | `tools/` | ~half day | `chore: tighten mypy on tools modules` |

**Not a PR — chore:**
- N5 — Prune 5 worktrees: `git worktree list` to confirm none have unmerged work, then `git worktree remove <name>` for each of `blissful-matsumoto-5f8af7`, `friendly-chatterjee-862e1c`, `nervous-golick-0ac12c`, `vigorous-diffie-7500d6`, `wonderful-wiles-6ac1a5`, then `git worktree prune`.

**Don't open a PR — watch items:**
- N4 (two HTTP stacks) — trigger: a second tool starts using `requests` directly.
- O3 (`tools/media/_upload.py` at 446 lines) — trigger: next change inside `_upload.py` makes its size meaningfully worse.

**Suggested sequence:**

| Day | PR | Why |
|---|---|---|
| 1 | PR 1 | Tiny; frees `shopify_client.py` for the typing pass |
| 1 | PR 2 | Independent of PR 1 and PR 3 — safe to land in parallel |
| 1 | Worktree prune | No review, knock out anytime |
| 2 | PR 3 (or 3a) | After PR 1 lands so its new helper gets typed in the same pass |
| 3 | PR 3b *(if split)* | After 3a ratchets the gate up cleanly |

After PR 1, PR 2, and PR 3 land, **active backlog goes to zero**; only watch items remain.

### Closed since this audit (added 2026-04-24)

All three PRs from the Remediation PRs table merged the same day:

| Plan slot | PR | Item | Status |
|---|---|---|---|
| PR 1 | [#40](https://github.com/rcchirwa/shopify-mcp/pull/40) (`16dec6d`) | O4 — inline `msgs.join` in `inventory.py:325` | closed |
| PR 2 | [#41](https://github.com/rcchirwa/shopify-mcp/pull/41) (`fe18d75`) | O2 — `tools/media/__init__.py` re-export shim | closed |
| PR 3 | [#42](https://github.com/rcchirwa/shopify-mcp/pull/42) (`6167ed6`) | O1 — mypy permissive baseline (`disallow_untyped_defs = true`) | closed |

Plus two transient hygiene items closed by the PR landing this very subsection:
- **Q1** — `TECH_DEBT.md` was untracked in git between creation (2026-04-23) and the commit landing this section. Closed by adding the file to git.
- **Q2** — `.gitignore` had uncommitted local diff for ~1 day adding entries for personal accomplishment-export artifacts. Closed by committing the diff. Used globs (`ACCOMPLISHMENTS_*.md` / `accomplishments_*.html`) rather than date-locked filenames so future weeks don't require ledger churn.

**Worktree cleanup (N5):** `git worktree list` now shows only the main worktree. The 5 entries listed in the original "Not a PR — chore" section have all been pruned. N5 fully closed.

### New items (added 2026-04-24, pending next full triage)

| # | Item | Where | Score |
|---|------|-------|-------|
| Q3 | `shopify_client.py` becoming a utility grab-bag — 265 lines, 10+ public exports across 3 concerns (transport, helpers, polling). Not debt yet; flag for trajectory. Trigger: next shared helper landing. | `shopify_client.py` | 9 (watch) |
| Q4 | `format_user_errors_joined()` has only one caller — built slightly ahead of the third use case. Note-only; future bulk-op callers will adopt naturally. | `shopify_client.py:226-243` | 4 |
| Q5 | `dict[str, Any]` baseline for GraphQL payloads — typing gate green but payloads are shallow. TypedDicts deferred per [#42](https://github.com/rcchirwa/shopify-mcp/pull/42). Trigger: first payload-shape bug mypy can't catch. | `tools/**/*.py` | 4 (watch) |

Active backlog after the post-#42 baseline: **zero**. Only watch / note-only items remain.

---

## 2026-04-23 — Re-triage

Baseline was the 2026-04-22 audit (below). 15 PRs merged between runs.

### Closed since last triage

| # | Item | How it closed |
|---|------|---------------|
| 1 | **Unpinned deps** (was 30) | `pyproject.toml` uses `>=floor,<next-major` on every dep incl. `setuptools<83`. `requirements.txt` / `requirements-dev.txt` consolidated into pyproject. (#29, #30, #31) |
| 5 | **Missing `test_discounts_offline.py`** (was 20) | 17 tests, 9.6 KB. Also added `test_orders_offline.py` (13) and `test_naming_offline.py` (24). |
| 6 | **Hardcoded API version `2024-10`** (was 20) | Bumped to `2026-01` at `shopify_client.py:72` with comment documenting the floor (`InventoryLevel.available` removal). |
| — | **`sys.path` prelude in test files** (memory follow-up) | Closed by #29 — repo now installable via `pip install -e .`. |
| — | **`FakeClient` re-export** (memory follow-up) | Closed by #28 — `_testing/__init__.py` re-exports. |
| — | **CI coverage gap** (stale memory) | CI now runs all `test_*_offline.py` under coverage with `--fail-under=100` at `.github/workflows/test.yml:30`. |

Two memory files are now stale and should be pruned:
- `project_followup_testing_module.md` (2/3 items done; third is condition-gated)
- The session 1 CI-coverage follow-up entry

### Unchanged or worse

| # | Item | Δ | Score |
|---|------|---|-------|
| 2 | **`userErrors` extraction duplicated** | count ~12 → **15** across 7 files; `tools/media.py:169` invented a third format (`_fmt_media_user_errors` adds `stage=…`), and `tools/products.py:851` has a local `_fmt` helper. Three flavors of the same concern now. | **22** (was 20) |
| 7 | **Preview/confirm gate duplicated** | `"To apply, call again with confirm=True"` appears **28×** (up from 18) — `tools/media.py` added 5 more write tools. | **20** (was 18) |
| 4 | **SEO constants in `products.py` not `validators/`** | unchanged — `tools/products.py:18-19`. | **20** |
| 3 | **No lint/type-check in CI** | still no ruff/mypy/black. But `--fail-under=100` coverage gate landed, which is a strong correctness signal in lieu of types. | **15** (was 20) |
| 8 | **`test_webhooks.py` not in pyproject ignore** | unchanged. CI safe (explicit glob); only local `pytest` without creds is affected. | **15** |

### New items found in added code

| # | Item | Where | I | R | E | Score |
|---|------|-------|---|---|---|-------|
| N1 | **`tools/media.py` is 917 lines** — now the largest module (was `products.py` at 884). 5 write tools, 10 private helpers, SSRF guard, two HTTP clients in play. Split candidate: `media_read.py` + `media_write.py` + `_upload.py`. | `tools/media.py` | 3 | 2 | 3 | 15 |
| N2 | **Duplicate job-poll timeouts** — `_JOB_POLL_TIMEOUT_S = 10` in `media.py:39` with comment "same as `collections.py`". Hand-synchronized constants in two files = drift risk. Move to `shopify_client.poll_job` default or shared constant. | `media.py:39`, `collections.py` | 2 | 3 | 1 | 20 |
| N3 | **SSRF guard in `media.py` only** — `_reject_if_private_host` (`media.py:195`) is load-bearing but scoped to one module. The next URL-accepting tool will silently skip it. The SSRF regression test (`test_media_offline.py`) guards the media path only; nothing forces new tools to adopt the guard. | `media.py:195` | 2 | 4 | 2 | **24** |
| N4 | **`requests` + `gql` both used** — `media.py:22` imports `requests` for staged uploads (justified: GraphQL can't do the PUT). No shared retry / timeout / User-Agent policy. Acceptable for v1 with one caller; flag for when a second use lands. | `media.py` | 1 | 2 | 3 | 9 |
| N5 | **Orphaned worktrees** — `.claude/worktrees/` contains 8+ abandoned branches. Repo hygiene, not code debt. | `.claude/worktrees/` | 1 | 1 | 1 | 10 |

### Current priority list

| Rank | Item | Score | Status |
|------|------|-------|--------|
| 1 | N3 — SSRF guard scope | 24 | new |
| 2 | #2 — `userErrors` extraction (3 flavors) | 22 | worse |
| 3 | N2 — Duplicate job-poll timeout constant | 20 | new |
| 4 | #7 — Preview/confirm gate (28×) | 20 | worse |
| 5 | #4 — SEO constants misplaced | 20 | unchanged |
| 6 | N1 — `media.py` 917 lines | 15 | new |
| 7 | #3 — Lint/type-check in CI | 15 | downgraded |
| 8 | #8 — `test_webhooks.py` pyproject ignore | 15 | unchanged |
| 9 | N5 — Worktree cleanup | 10 | new |
| 10 | N4 — Two HTTP stacks, no shared policy | 9 | watch |

### Phased plan

**This week (~1.5 days):**
- **N3** — Move `_reject_if_private_host` to `shopify_client.py` (or a new `_url_safety.py`) even with one caller today. Shipping the guard + regression test in the same PR means the next URL-accepting tool author won't see the guard unless it's discoverable outside `media.py`.
- **#4** — Move `SEO_TITLE_MAX_CHARS` / `SEO_DESCRIPTION_MAX_CHARS` to `validators/`. ~5 min.
- **N2** — Change `poll_job` signature to `timeout_s=10` default; delete per-module `_JOB_POLL_TIMEOUT_S`. ~15 min.
- **#8** — Add `test_webhooks.py` to `pyproject.toml` `addopts` ignore list. ~1 min.

**Next week (~2 days):**
- **#2 + #7 as a pair.** Land `ShopifyClient.mutate(query, vars, root) -> (payload, err)` first, then a `gate(preview, confirm, tool_name, apply_fn, summary_fn)` helper in `tools/_confirm.py`. Do #2 before #7 so the gate's `apply_fn` can return `err` directly. This is the same recommendation as the earlier DRY pass — just more urgent now that `media.py` made the pattern drift in three directions.

**Backlog (don't pre-refactor):**
- **N1** — Split `media.py` when you next touch it, not before.
- **#3** — Add `ruff check` to CI workflow.
- **N5** — Prune abandoned `.claude/worktrees/` (cosmetic).

### Net assessment

4 of the top 6 items closed in one week. The duplication items didn't get fixed and now have ~30% more instances plus one variant drift — that's the compounding kind of debt and belongs in the next PR batch. New architectural debt is small (N1, N3) and no new categories appeared. Good trajectory.

---

## 2026-04-22 — Initial audit

### High priority (score ≥ 20)

| # | Item | I | R | E | Score |
|---|------|---|---|---|-------|
| 1 | Unpinned deps — `requirements.txt` used `mcp>=1.0.0`, `gql>=…`, `pytest>=7` | 2 | 4 | 1 | 30 |
| 2 | Duplicate `userErrors` extraction — same 3-line pattern ~12× across tools | 3 | 2 | 2 | 20 |
| 3 | No lint/type-check in CI — `test.yml` only ran `pytest` | 3 | 2 | 2 | 20 |
| 4 | Duplicate SEO constants — `SEO_TITLE_MAX_CHARS=70`, `SEO_DESCRIPTION_MAX_CHARS=160` in `tools/products.py:17-18`, not shared with `validators/naming.py` | 2 | 2 | 1 | 20 |
| 5 | Untested write paths — no `test_discounts_offline.py`; `create_discount_code` had no offline coverage | 2 | 3 | 2 | 20 |
| 6 | Hardcoded API version `2024-10` in `shopify_client.py:50` with no abstraction | 2 | 3 | 2 | 20 |

### Medium (15–19)

| # | Item | I | R | E | Score |
|---|------|---|---|---|-------|
| 7 | Preview/confirm flow duplicated across ~18 write tools | 4 | 2 | 3 | 18 |
| 8 | `test_webhooks.py` not excluded from local `pytest` — only `test_shopify_mcp.py` in `pyproject.toml:5` | 2 | 1 | 1 | 15 |

### Low

- `tools/products.py` at 884 lines, 15 tools — split when it next grows. (9)
- No release automation / `__version__` — internal tool. (6)

### DRY pass — recommended refactors

Captured in conversation, three actions:
1. Add `ShopifyClient.mutate(query, variables, root) -> (payload, err)` and delete the 12 copies of the userErrors-join pattern.
2. Add `gate(preview, confirm, tool_name, apply_fn, summary_fn)` in `tools/_confirm.py` to collapse the 18 preview/confirm sites.
3. Move SEO constants from `tools/products.py` into `validators/`.

Do (1) before (2) so `apply_fn` returns `err` directly from `client.mutate()`.

Explicitly **not** DRYed (rule-of-three / readability tradeoffs):
- `from_gid`-normalize-input idiom (~15× but 3 chars each).
- `userErrors { field message }` in GraphQL query strings (fragments hurt grep).
- Local response formatters like `_endpoint_url` — right scope.

---

## How to use this file

This file is a **journal**, not a snapshot. Each dated section is one triage event plus any follow-up notes added during the days that follow it. Future sessions should be able to pick up the work from any reference (item ID, PR number, date) without losing context.

### Conventions

- **One dated section per triage.** Newest at the **top**. Format: `## YYYY-MM-DD — Re-triage` (or `Initial audit` for the first one).
- **Standard subsections inside each audit, in this order:**
  1. *(brief preamble)* — what changed since the prior audit, baseline reference
  2. `### Closed since last triage (YYYY-MM-DD)` — what got fixed, with commit shas
  3. `### Unchanged or worse` — items that didn't move
  4. `### New items found` — debt that surfaced in new code
  5. `### Current priority list` — full ordered backlog as of this audit
  6. `### Phased plan` — week-by-week recommendation
  7. `### Net assessment` — qualitative summary
- **Follow-up subsections are allowed** under any audit between triages. Date-stamp them: `### Remediation PRs (added YYYY-MM-DD)`, `### Decision log (added YYYY-MM-DD)`, `### Mid-cycle note (added YYYY-MM-DD)`. This is how the journal accumulates context without waiting for the next full triage.
- **Stable IDs.** Items keep their original ID forever (e.g. `O2`, `N3`, `#7`). When an item closes, the *next* audit's "Closed since last triage" table references it by that ID. Don't renumber.
- **PR numbers are local to one audit.** "PR 2" inside the 2026-04-24 audit is unrelated to a "PR 2" in a future audit. When referencing a PR in chat, name the audit date too: *"PR 2 from the 2026-04-24 audit."*
- **Don't rewrite history.** Prior dated sections are the audit trail. Errata go in a follow-up subsection under the original audit, not as edits to it.

### Workflow

- **To run a re-triage:** invoke `/engineering:tech-debt` with the prompt *"Re-triage against `TECH_DEBT.md`."* The skill will diff against the latest dated section and write a new one at the top.
- **To log a decision between triages:** add a date-stamped follow-up subsection under the latest audit (`### Decision log (added YYYY-MM-DD)`).
- **To reference an item from chat:** quote its stable ID (e.g. *"working on O2 today"*) — Claude can grep this file for it.
- **Memory hygiene:** memory-file follow-ups that this ledger supersedes should be pruned from `~/.claude/projects/-Users-robertchirwa-shopify-mcp/memory/` after each triage.
