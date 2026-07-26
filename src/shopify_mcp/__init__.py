"""``shopify_mcp`` — the single top-level name this distribution installs.

Everything the project ships lives under this namespace (Story 10.47 / FS-3).
The flat layout that preceded it installed six generic top-level names —
``depcheck``, ``logging_config``, ``settings``, ``shopify_client`` and the
``tools``/``validators``/``shopify`` packages — onto the import path of any
environment that installed this project. ``shopify`` in particular is the
top-level module owned by the **ShopifyAPI** distribution on PyPI, so the two
contended for one name and the loser was decided by ``sys.path`` order: a
silent wrong import rather than an error.

Deliberately empty of re-exports. Importing the package must not drag in the
MCP stack, ``requests``, or ``gql`` — ``shopify.queries`` is pure data and
``shopify.operations`` is callable without a server, and a convenience
re-export here would quietly couple every consumer to the heaviest module in
the tree. Import the module you want: ``from shopify_mcp.shopify.operations
import products``.

Layout::

    shopify_mcp/
    ├── server.py       # MCP entry point (console script: shopify-mcp)
    ├── client.py       # Shopify Admin GraphQL client
    ├── settings.py · logging_config.py · depcheck.py
    ├── shopify/        # domain layer, independent of the MCP surface
    ├── tools/          # MCP tool surface
    └── validators/

Enforced by ``tests/architecture/test_src_layout.py``.
"""
