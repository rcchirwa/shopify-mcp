# Tech Debt Ledger

Living record of the technical-debt triage for `shopify-mcp`. Newest entry first. Future Claude sessions: read the latest dated section and work forward — older sections are kept for trend context only.

Scoring: `Priority = (Impact + Risk) × (6 − Effort)`, each axis 1–5, effort inverted.

**Last full audit:** 2026-04-24. **Last follow-up:** 2026-04-24.

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
