"""Structural type for the GraphQL client the operations layer depends on.

A ``Protocol`` (not a concrete import of ``ShopifyClient``) keeps
``shopify.operations`` decoupled from both the MCP server and the rest of the
codebase: any object exposing ``execute`` + ``paginate`` satisfies it. Both
``shopify_client.ShopifyClient`` and the test ``FakeClient`` match structurally,
so operations are callable from non-MCP entry points and tests alike.
"""

from typing import Any, Protocol


class GraphQLClient(Protocol):
    """Minimal GraphQL client surface used by ``shopify.operations``."""

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> Any: ...

    def paginate(
        self,
        query_str: str,
        variables: dict[str, Any],
        *,
        connection_path: list[str],
        page_size: int = 50,
        max_pages: int = 10,
    ) -> tuple[dict[str, Any], list[Any], bool]: ...
