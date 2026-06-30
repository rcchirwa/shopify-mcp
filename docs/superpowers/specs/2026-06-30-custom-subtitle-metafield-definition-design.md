# `custom.subtitle` Metafield Definition — Design / Runbook

**Date:** 2026-06-30
**Status:** Approved (design)
**Type:** Operational runbook (no code change to the MCP server)

## Context

A product story requires a `custom.subtitle` metafield on Products — a short subtitle /
tagline rendered under the product title on the storefront (AC #1: *create the
`custom.subtitle` metafield definition, single-line text*).

A metafield **value** can be written to any product without a definition, but AC #1 asks
for the **definition** itself, which controls the field's type, validation, admin-UI
visibility, and storefront access. Creating a definition requires the
`metafieldDefinitionCreate` Admin GraphQL mutation.

This MCP server (`shopify-aon`) exposes metafield **value** tools
(`set_/get_/delete_product_metafields`) but **does not** surface
`metafieldDefinitionCreate` — definitions are explicitly deferred in
`architectural_tech_debt.md`. Rather than add tooling, we satisfy AC #1 the simplest
proportionate way: a metafield definition is **store-level config created once per
store**, so it is created manually in Shopify Admin and recorded here for repeatability
across environments.

**Intended outcome:** the `custom.subtitle` definition exists in the target store(s),
storefront-readable, length-capped, and verified to be live via the MCP's existing value
tools.

## Definition specification

| Field | Value |
|---|---|
| Namespace and key | `custom.subtitle` (namespace `custom`, key `subtitle`) |
| Name (display) | `Subtitle` |
| Description | `Short subtitle / tagline shown under the product title.` |
| Owner type | Product |
| Type | Single line text (`single_line_text_field`) |
| Validation | Maximum length **80** characters |
| Storefront access | **Enabled** (Liquid theme / Storefront API can read it) |
| Admin API access | Read/write (default) |

## Procedure (Shopify Admin)

1. Shopify Admin → **Settings → Custom data → Products**.
2. Click **Add definition**.
3. **Name:** `Subtitle`. Confirm the auto-generated namespace and key resolves to
   `custom.subtitle` (edit the namespace/key if Admin defaults to something else).
4. **Description:** `Short subtitle / tagline shown under the product title.`
5. **Select type** → **Single line text**.
6. Under the type's validation options, set **Maximum character length = 80**.
7. Enable **Storefront** access (so the theme / Storefront API can read the value).
8. Leave Admin API access at its default (read/write).
9. **Save**.

## Verification

No metafield-definition **read** tool exists in this server, so verify behaviorally using
the existing value tools against a throwaway/test product GID.

1. **Validation is live** — call `set_product_metafields` with `custom.subtitle` set to a
   string **longer than 80 characters** (`confirm=True`). Expect Shopify to **reject** it
   with a length `userError`. This proves the definition + validation are active (a value
   with no definition would have been accepted).
2. **Valid write succeeds** — call `set_product_metafields` with a short valid value.
   Expect success.
3. **Read-back** — call `get_product_metafields` filtered to namespace `custom`. Confirm
   `subtitle` returns with `type: single_line_text_field` and the value from step 2.
4. **Admin recognizes it** — open the test product in Admin; the Metafields section shows
   **Subtitle** as a typed field (not a raw/undefined entry).
5. Clean up the test value if desired (`delete_product_metafields`).

## Out of scope

- Adding a `create_product_metafield_definition` MCP tool (definitions remain deferred per
  `architectural_tech_debt.md`).
- Writing subtitle **values** to real products, and any theme/Liquid work to render the
  subtitle — those are separate ACs.

## Per-store note

Repeat the **Procedure** in every store/environment that needs the field (e.g. dev vs.
production). The definition is store-scoped; nothing in this repo creates it
automatically.
