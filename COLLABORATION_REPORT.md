# Shopify MCP Server — Collaboration Report

**Project:** Custom Shopify MCP Server for Claude Desktop
**Store:** wizhardhittin.myshopify.com — Global Streetwear Syndicate
**Sub-brands:** All or Nothing Cypher · Vanish Clothing
**Completed:** April 2026
**Repo:** https://github.com/rcchirwa/shopify-mcp

---

## What Was Built

A production-ready Python MCP (Model Context Protocol) server that connects Claude Desktop directly to a live Shopify store via the Admin REST API. Claude can now interact with products, inventory, collections, discount codes, and orders through natural language — without any manual API calls or Shopify Admin UI navigation.

**13 tools exposed to Claude:**
- 5 product tools (list, read, update title, update description, list by collection)
- 2 inventory tools (read levels, update quantity)
- 2 collection tools (read, update)
- 2 discount tools (list, create)
- 2 order tools (list, read single)

---

## Why This Was Built

The store runs **monthly merch drops** tied to hip-hop cypher YouTube videos. Each drop involves:
- Bulk-renaming product titles to match AON/Vanish brand naming conventions
- Checking inventory across variants before and after launch
- Pulling order data to understand traffic sources and post-drop analytics

The core business problem: **product titles feed directly into GA4 ecommerce event parameters**. A wrong title in Shopify becomes a corrupted event in GA4 — and GA4 data cannot be fixed retroactively. The naming convention validator was designed specifically to catch these errors before they reach production.

Previously, all of this required manually navigating Shopify Admin or writing one-off scripts. The MCP server eliminates that entirely — every task above can now be done through Claude chat in seconds.

---

## How We Worked Together

### Phase 1 — Scoping the prompt

Before writing any code, Robert created a detailed prompt document (`Shopify_MCP_Claude_Code_Prompt.md`) specifying:
- The exact tools needed, grouped by domain (products, inventory, collections, discounts, orders)
- Safety requirements (confirm pattern for writes, handle protection, audit logging)
- Naming convention validation rules with format examples
- File structure, tech stack, and credentials handling approach
- GitHub publishing requirements and a post-publish collaboration report

This upfront scoping work was the most important step. Because the requirements were precise and well-organized, the implementation could be done in a single session with no back-and-forth on what to build.

**Key lesson:** A well-structured prompt document is itself a professional deliverable. It functions as a technical spec, and the quality of the output directly reflects the quality of the spec.

### Phase 2 — Environment diagnosis

The first technical challenge was the environment. The shell session showed:
- Homebrew: not in PATH
- Python: 3.9.6 (below the 3.10+ requirement for the MCP SDK)

Rather than blocking on this, the decision was made to write all source files immediately (independent of Python version) while Robert installed the dependencies. This parallelized the work effectively.

When Robert confirmed the installs were done, a second environment check found Homebrew and Python 3.11 at `/opt/homebrew/` — they were installed correctly, just not on the current shell PATH. The venv was created using the full absolute path `/opt/homebrew/bin/python3.11` rather than relying on PATH resolution.

**Key lesson:** On macOS with Apple Silicon, Homebrew installs to `/opt/homebrew/` not `/usr/local/`. Always check both paths when `brew` or `python3.11` isn't found.

### Phase 3 — Architecture decisions

Several design choices were made deliberately:

**Modular tool registration pattern**
Each domain (products, inventory, etc.) lives in its own file and exposes a `register(server, client)` function. This makes it easy to add or remove tool groups, and keeps each file focused on a single concern. The main entry point (`shopify_mcp.py`) is thin — it just wires everything together.

**Shared ShopifyClient class**
All HTTP communication goes through a single `ShopifyClient` instance passed to each tool module. This means credentials are loaded once, error handling is centralized, and if Shopify's API base URL ever changes, there's one place to update it.

**Preview + confirm pattern**
Every write operation returns a formatted preview when called without `confirm=True`. This was a deliberate choice for an AI-assisted workflow — Claude might be used by someone who isn't deeply technical, and accidental writes to a live Shopify store could have real business impact. The preview also gives Claude a chance to show the user exactly what will change before anything touches production.

**Write operation audit log**
`aon_mcp_log.txt` records every confirmed write with a UTC timestamp, tool name, and change description. In a drop-based business where timing matters, this log is a lightweight audit trail — useful for debugging if a product title was changed to the wrong value, or if an inventory update happened at the wrong time.

**Naming validator as a separate module**
The AON/Vanish naming convention validator lives in `validators/naming.py` — not inside the product tool. This separation means the validation logic can be tested independently and reused if other tools (e.g., bulk rename) are added later.

### Phase 4 — The connection test

Running `test_shopify_mcp.py` confirmed:
```
Connected to: Global Streetwear Syndicate (wizhardhittin.myshopify.com)
Plan: basic
Currency: USD
Connection test PASSED.
```

The test also pulled 3 live products, revealing something immediately useful: the first two products had non-compliant titles:
- `"Fall Into Greatness" Vanish Iconic V Large Logo Hoodie` — contains quotes
- `"Fall Into Greatness' Vanish Iconic V Logo Shorts` — contains quotes and a mismatched quote character
- `"V"  Logo Tee` — contains quotes, double spaces, and no brand prefix

This validated that the naming convention validator is addressing a real, active problem — not a hypothetical one.

### Phase 5 — GitHub publishing

The repo was published to https://github.com/rcchirwa/shopify-mcp with:
- `.env` excluded via `.gitignore` (credentials never committed)
- `.env.example` committed with placeholder values for onboarding
- `aon_mcp_log.txt` excluded (local write log, not source code)
- A full `README.md` covering setup, tools, scopes, naming conventions, and example usage
- One clean initial commit with a descriptive message

---

## Technical Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| MCP SDK | `mcp` 1.27.0 (Anthropic official Python SDK) |
| HTTP client | `requests` 2.33 |
| Credentials | `python-dotenv` 1.2 |
| Shopify API | Admin REST API 2024-01 |
| Transport | stdio (standard MCP transport for Claude Desktop) |
| Version control | Git + GitHub CLI (`gh`) |

---

## Challenges and How They Were Resolved

| Challenge | Resolution |
|---|---|
| Homebrew/Python not on shell PATH | Used full absolute paths (`/opt/homebrew/bin/python3.11`) throughout |
| Needing 3.10+ for MCP SDK, only 3.9.6 on system | Installed Python 3.11 via Homebrew; created venv with explicit path |
| Claude Desktop config already existed (non-empty) | Read the existing file first, merged `mcpServers` block into existing JSON rather than overwriting |
| Shopify Admin API returns collections as either `custom_collections` or `smart_collections` | `_resolve_collection()` helper tries both endpoints and returns whichever matches |
| Inventory requires separate API call per variant | `get_inventory` fetches product first, then calls `/inventory_levels.json` for each variant's `inventory_item_id` |

---

## What This Demonstrates (Resume / LinkedIn framing)

**For a technical audience:**
- Built a production MCP server in Python using the official Anthropic MCP SDK, enabling Claude to interact with a live Shopify store through natural language
- Designed a safety-first write pattern (preview + confirm) and audit logging system for AI-assisted e-commerce operations
- Structured the server as a modular, domain-separated codebase with a shared API client, separate tool registration, and an isolated validation layer
- Shipped a GitHub-ready repo with `.env` handling, `.env.example`, full README, and clean commit history

**For a general / LinkedIn audience:**
- Connected Claude AI directly to a Shopify store, enabling product management, inventory checks, and order reviews through chat — no manual API calls
- Built a custom AI tool for a streetwear brand's monthly drop workflow, addressing a real analytics integrity problem where incorrect product names corrupted GA4 data
- Went from zero to a live, GitHub-published tool in a single session using Claude Code as an AI development partner

**Resume bullet points:**
- Developed a custom Python MCP server integrating Claude Desktop with Shopify Admin REST API, exposing 13 tools covering products, inventory, collections, discounts, and orders
- Implemented safety-first AI tooling patterns including preview/confirm writes, handle protection, and UTC-timestamped audit logging
- Built a brand naming convention validator enforcing AON/Vanish product title formats to prevent downstream GA4 analytics corruption
- Published open-source to GitHub with full documentation, `.env.example` onboarding, and clean credential hygiene

---

## What Was Learned

1. **MCP servers are simpler than they look.** The Anthropic Python MCP SDK handles all the protocol complexity. The developer's job is just to write functions decorated with `@server.tool()` — the framework handles serialization, tool discovery, and transport.

2. **Safety in AI tooling is a design decision, not an afterthought.** The preview + confirm pattern was specced upfront, not added later. In an AI-assisted workflow where Claude might be invoked by a non-technical user, the cost of an accidental write to a live store is real. Building the guardrail into the API surface (require `confirm=True`) is more reliable than relying on prompt instructions.

3. **The prompt document is a forcing function for clear thinking.** Writing a structured prompt spec before starting pushed the scoping work to happen upfront — what tools, what safety rules, what naming conventions, what file structure. The implementation session had no ambiguity because the spec had no ambiguity.

4. **Environment issues on macOS are predictable.** Apple Silicon Macs install Homebrew to `/opt/homebrew/`, not `/usr/local/`. Homebrew-managed binaries won't be on PATH until the shell profile is reloaded. Knowing this pattern means diagnosis takes seconds rather than minutes.

5. **Real data validates design.** The connection test pulled live products and immediately showed non-compliant titles with mismatched quotes — exactly the problem the naming validator was built to catch. Shipping something and immediately seeing it address a real issue in production data is a strong signal that the design was right.

---

## Next Steps (potential expansions)

- **Bulk rename tool** — apply naming convention corrections to a list of product IDs in a single call
- **Drop readiness check** — a single tool that runs pre-drop checks: inventory levels, title compliance, collection membership, discount code status
- **Webhook listener** — extend the server to listen for Shopify order webhooks and surface them in Claude
- **GraphQL migration** — Shopify is deprecating REST in favor of GraphQL Admin API; migrating `shopify_client.py` to GraphQL would future-proof the server

---

*This report was generated at the close of the build session as a knowledge base artifact for portfolio, LinkedIn, and resume use.*
*Built with Claude Code (claude-sonnet-4-6) · April 2026*
