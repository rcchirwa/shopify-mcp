"""
``shopify`` — Shopify domain layer, independent of the MCP tool surface.

Layering rule (one-way, no cycles)::

    tools/  ->  shopify.operations  ->  shopify.queries

- ``shopify.queries`` holds GraphQL strings grouped by resource, reusable via
  shared fragments. Pure data; imports nothing from the rest of the codebase.
- ``shopify.operations`` holds typed business-logic wrappers (e.g.
  ``update_product_title(client, product_id, ...) -> dict``). They take a
  duck-typed GraphQL client (see ``shopify._client.GraphQLClient``) and are
  callable WITHOUT the MCP server — usable from CLI, scripts, or tests.
- ``tools`` keeps param coercion, the preview/confirm flow, and output
  formatting; it calls into ``shopify.operations``.

``shopify`` MUST NOT import from ``tools`` — the dependency is strictly one-way
so the domain layer stays reusable from non-MCP entry points and no import
cycle can form. This is enforced by ``test_shopify_layering_offline.py``.

Status: Story 10.23 / A5 establishes this structure and migrates the
``products`` domain as the pilot. Remaining domains migrate one per PR.
"""
