"""Typed business-logic wrappers over ``shopify.queries``.

Each operation takes a duck-typed GraphQL client (``shopify._client.GraphQLClient``)
and is callable without the MCP server, enabling non-MCP entry points (CLI,
scripts). Operations contain GID coercion, query/mutation execution, and input
building — no MCP imports and no output formatting (that stays in ``tools``).
"""
