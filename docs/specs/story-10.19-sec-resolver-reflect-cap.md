# Story 10.19 — SEC-resolver-reflect-cap

Apply the existing `_GID_DISPLAY_MAX` reflection cap consistently to every user-input
reflection site in `tools/catalog_hygiene.py`. Closes `SEC-resolver-reflect-cap`
(TECH_DEBT.md, 2026-05-28 follow-up, Score 4, note-only — internal MCP tool, low risk).

**Plan author:** Opus 4.8 · **Implementer:** Sonnet · **Epic:** 10 — Tech Debt

---

## Section A — Audit Findings

`_GID_DISPLAY_MAX = 200` already exists (`tools/catalog_hygiene.py:238`) and is applied at
4 sites. The cap is **not** applied at the handle/taxonomy/owner/metafield reflection sites
where user-controlled input is interpolated into error strings unbounded — enabling
log-flooding / error-echo amplification via a multi-KB input.

### Already capped (will migrate to a shared `_cap()` helper for one source of truth)

| # | File | Line | Current code |
|---|------|------|--------------|
| C1 | tools/catalog_hygiene.py | 1555 | `got non-Product GID: {stripped[:_GID_DISPLAY_MAX]!r}` |
| C2 | tools/catalog_hygiene.py | 1560 | `Empty product GID body: {stripped[:_GID_DISPLAY_MAX]!r}` |
| C3 | tools/catalog_hygiene.py | 1782 | `got non-Product GID: {stripped[:_GID_DISPLAY_MAX]!r}` |
| C4 | tools/catalog_hygiene.py | 1787 | `Empty product GID body: {stripped[:_GID_DISPLAY_MAX]!r}` |

### Uncapped — PRIMARY scope (handle / taxonomy — the named SEC-resolver-reflect-cap sites)

| # | File | Line | Current code | Context |
|---|------|------|--------------|---------|
| 1 | tools/catalog_hygiene.py | 1575 | `No product found with handle {stripped!r}.` | `_resolve_product_gid` handle-not-found |
| 2 | tools/catalog_hygiene.py | 1604 | `Empty TaxonomyCategory GID body: {stripped!r}` | `_resolve_taxonomy_category` GID body |
| 3 | tools/catalog_hygiene.py | 1625 | `No taxonomy categories matched search {stripped!r}.` | taxonomy 0-result |
| 4 | tools/catalog_hygiene.py | 1649 | `{stripped!r} by fullName or name.` | exact: no match |
| 5 | tools/catalog_hygiene.py | 1656 | `matched {stripped!r} exactly — refine the search.` | exact: >1 match |
| 6 | tools/catalog_hygiene.py | 1674 | `taxonomy categories matched {stripped!r} — refine…` | reject-ambiguous |

### Uncapped — EXTENSION scope (owner / metafield resolvers — same vuln class, recommended)

| # | File | Line | Current code | Context |
|---|------|------|--------------|---------|
| 7 | tools/catalog_hygiene.py | 900 | `ownerId has empty GID body: {stripped!r}` | `_parse_owner_gid` |
| 8 | tools/catalog_hygiene.py | 902 | `ownerId must be a Product or ProductVariant GID (got {stripped!r})` | `_parse_owner_gid` |
| 9 | tools/catalog_hygiene.py | 1101 | `metafieldId must be a Metafield GID (got {stripped!r})` | `_parse_metafield_gid` |
| 10 | tools/catalog_hygiene.py | 1103 | `metafieldId has empty GID body: {stripped!r}` | `_parse_metafield_gid` |
| 11 | tools/catalog_hygiene.py | 1131 | `ownerId has empty GID body: {stripped!r}` | `_resolve_owner_gid_for_metafield` Product |
| 12 | tools/catalog_hygiene.py | 1135 | `ownerId has empty GID body: {stripped!r}` | `_resolve_owner_gid_for_metafield` Variant |
| 13 | tools/catalog_hygiene.py | 1142 | `ownerId {stripped!r} is ambiguous — supply a Product or ProductVariant…` | numeric ambiguous |

### Discrepancies vs TECH_DEBT.md

- The 2026-05-28 forward note named "lines ~1570, 1620, 1644, 1651, 1669" (5 sites). The
  actual current handle/taxonomy set is **6** sites (1575/1604/1625/1649/1656/1674) — line
  numbers drifted since the note and it missed the `Empty TaxonomyCategory GID body` site.
- The note scoped only handle/taxonomy. Investigation found **7 additional** owner/metafield
  reflection sites with the identical unbounded pattern (#7–#13). Excluding them would
  reproduce exactly the half-applied-cap inconsistency the prior `/gss-dual-review` follow-up
  (commit b8ce59d) flagged. Recommend including them — hence "all 13 + migrate 4".
- The handle path in `_resolve_owner_gid_for_metafield` (line 1148) delegates to
  `_resolve_product_gid`, so capping #1 transitively covers it — no separate site.
- `Handle lookup failed (...): {e}` (line 1572) reflects an **exception** message, not raw
  user input — out of scope, left unchanged.

---

## Section B — Implementation Steps

1. tools/catalog_hygiene.py — directly below the `_GID_DISPLAY_MAX = 200` definition (line ~238), add a module-level helper `def _cap(s: str) -> str:` returning `s[:_GID_DISPLAY_MAX]`, with a one-line docstring noting it bounds user-supplied text reflected into error messages (log-flood / echo defence).
2. tools/catalog_hygiene.py line 1555 — replace `stripped[:_GID_DISPLAY_MAX]` with `_cap(stripped)`.
3. tools/catalog_hygiene.py line 1560 — replace `stripped[:_GID_DISPLAY_MAX]` with `_cap(stripped)`.
4. tools/catalog_hygiene.py line 1782 — replace `stripped[:_GID_DISPLAY_MAX]` with `_cap(stripped)`.
5. tools/catalog_hygiene.py line 1787 — replace `stripped[:_GID_DISPLAY_MAX]` with `_cap(stripped)`.
6. tools/catalog_hygiene.py line 1575 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `No product found with handle` message.
7. tools/catalog_hygiene.py line 1604 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `Empty TaxonomyCategory GID body` message.
8. tools/catalog_hygiene.py line 1625 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `No taxonomy categories matched search` message.
9. tools/catalog_hygiene.py line 1649 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `resolve_strategy='exact' … by fullName or name` message.
10. tools/catalog_hygiene.py line 1656 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `matched … exactly — refine the search` message.
11. tools/catalog_hygiene.py line 1674 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `reject-ambiguous … taxonomy categories matched` message.
12. tools/catalog_hygiene.py line 900 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `ownerId has empty GID body` message in `_parse_owner_gid`.
13. tools/catalog_hygiene.py line 902 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `ownerId must be a Product or ProductVariant GID` message in `_parse_owner_gid`.
14. tools/catalog_hygiene.py line 1101 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `metafieldId must be a Metafield GID` message.
15. tools/catalog_hygiene.py line 1103 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `metafieldId has empty GID body` message.
16. tools/catalog_hygiene.py line 1131 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the Product-branch `ownerId has empty GID body` message in `_resolve_owner_gid_for_metafield`.
17. tools/catalog_hygiene.py line 1135 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the ProductVariant-branch `ownerId has empty GID body` message in `_resolve_owner_gid_for_metafield`.
18. tools/catalog_hygiene.py line 1142 — replace `{stripped!r}` with `{_cap(stripped)!r}` in the `ownerId … is ambiguous` message.
19. test_catalog_hygiene_offline.py — add `test_resolver_oversized_handle_is_capped_in_error`: feed a 10KB non-existent handle through a `_resolve_product_gid` path, assert `No product found with handle` present and the reflected slice does not exceed `_GID_DISPLAY_MAX` (assert `oversized[:_GID_DISPLAY_MAX + 1] not in out`).
20. test_catalog_hygiene_offline.py — add `test_resolver_oversized_taxonomy_search_is_capped_in_error`: feed a 10KB taxonomy search returning zero nodes, assert `No taxonomy categories matched` present and reflected slice ≤ `_GID_DISPLAY_MAX`.
21. test_catalog_hygiene_offline.py — add `test_resolver_oversized_owner_id_is_capped_in_error`: feed a 10KB non-prefixed ownerId, assert `ambiguous` or `must be a Product or ProductVariant GID` present and reflected slice ≤ `_GID_DISPLAY_MAX`.
22. test_catalog_hygiene_offline.py — add `test_resolver_oversized_metafield_id_is_capped_in_error`: feed a 10KB non-Metafield GID, assert `metafieldId must be a Metafield GID` present and reflected slice ≤ `_GID_DISPLAY_MAX`.
23. test_catalog_hygiene_offline.py — add a direct `_cap` unit test asserting `_cap("x" * (_GID_DISPLAY_MAX + 50))` has length exactly `_GID_DISPLAY_MAX` and `_cap("short")` is unchanged.
24. Run `ruff check . && ruff format --check .` — confirm zero errors.
25. Run `mypy tools/catalog_hygiene.py` — confirm clean.
26. Run `pytest --cov --cov-fail-under=100` — confirm all tests pass at 100% coverage.
27. Update TECH_DEBT.md — add a `### Follow-up (added 2026-06-04, Story 10.19)` subsection under the 2026-05-28 audit recording SEC-resolver-reflect-cap closed (all 13 reflection sites capped + 4 migrated to `_cap()`), and strike the `SEC-resolver-reflect-cap` forward-note line in the 2026-05-28 follow-up.
28. Open PR targeting main — title: `fix(security): cap user-input reflection in catalog_hygiene resolvers (Story 10.19 / SEC-resolver-reflect-cap)`.

---

## Section C — Acceptance Criteria

### Functional
- [ ] An oversized (10KB) handle reflected by `_resolve_product_gid` is truncated to ≤ `_GID_DISPLAY_MAX` chars in the error string.
- [ ] An oversized taxonomy search term reflected by `_resolve_taxonomy_category` is truncated to ≤ `_GID_DISPLAY_MAX` chars across all three resolve strategies.
- [ ] An oversized `ownerId` reflected by `_parse_owner_gid` and `_resolve_owner_gid_for_metafield` is truncated to ≤ `_GID_DISPLAY_MAX` chars.
- [ ] An oversized `metafieldId` reflected by `_parse_metafield_gid` is truncated to ≤ `_GID_DISPLAY_MAX` chars.
- [ ] All existing error-message substrings (e.g. `No product found with handle`, `metafieldId must be a Metafield GID`) remain unchanged for normal-length inputs.

### Asset & Code
- [ ] A single `_cap()` helper is the one source of truth; no remaining inline `stripped[:_GID_DISPLAY_MAX]` slices.
- [ ] All 13 previously-uncapped reflection sites now route through `_cap()`.
- [ ] The 4 previously-capped sites are migrated to `_cap()` (no behavioral change).
- [ ] No reflection of an exception message (`{e}`) was altered.

### CI / Theme-Check
- [ ] `ruff check .` and `ruff format --check .` pass with zero errors.
- [ ] `mypy` is clean.
- [ ] `pytest` passes at 100% coverage (`--cov-fail-under=100`).

### Documentation
- [ ] TECH_DEBT.md records SEC-resolver-reflect-cap as closed with a dated follow-up subsection.
- [ ] The SEC-resolver-reflect-cap forward-note line in the 2026-05-28 follow-up is struck through.

---

## Section D — Trello Card Metadata
- **Board:** hJSHw77Q
- **Epic:** Epic 10 — Tech Debt (card FinsjExf)
- **Story:** 10.19  (highest existing on board is 10.18 — confirmed via Epic card activity feed)
- **List:** Backlog
- **Suggested title:** `Story 10.19 - Cap user-input reflection in catalog_hygiene resolvers (SEC-resolver-reflect-cap)`
