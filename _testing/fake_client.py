"""Shared test doubles for offline tool tests.

Every `tools/<name>.py` module takes a FastMCP-like server and a GraphQL
client. Offline tests feed them a `CapturingServer` (records the decorated
tool functions so tests can invoke them directly) and a `FakeClient`
(scripted GraphQL responses). Extracted here to prevent drift across the
per-suite copies that were diverging.

A response item that is a `BaseException` instance is raised instead of
returned — lets a test assert exception-path handling without writing a
custom client subclass. `BaseException` (not `Exception`) because the
prior publications-suite copy already used `BaseException` and the media
copy used the narrower `Exception`; the wider check subsumes both so no
test's behavior changes.
"""

from collections.abc import Callable, Iterable
from typing import Any

from pydantic import SecretStr

from settings import Settings


def _default_test_settings() -> Settings:
    """Synthetic creds + default knobs. Tool tests don't need real values —
    they exercise the tool surface, not the HTTP client."""
    return Settings(
        shopify_store_url="test.myshopify.com",
        shopify_access_token=SecretStr("shpat_test00000000000000000000000"),
    )


class CapturingServer:
    """Stand-in for FastMCP that records decorated tool functions."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return deco


class FakeClient:
    """Scripted responses for `client.execute()`.

    Responses are consumed in order. A response that is a `BaseException`
    instance is raised rather than returned, so tests can assert on
    exception-path handling in write-path tools.
    """

    def __init__(self, responses: Iterable[Any], settings: Settings | None = None) -> None:
        self.responses: list[Any] = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        # Tools that consult client._settings (webhook allowlist, poll_job
        # backoff/timeout) need a real Settings here, not a sentinel.
        self._settings: Settings = settings or _default_test_settings()

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        self.calls.append((query, variables))
        if not self.responses:
            raise AssertionError("FakeClient: unexpected extra execute() call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def paginate(
        self,
        query_str: str,
        variables: dict[str, Any],
        *,
        connection_path: list[str],
        page_size: int = 50,
        max_pages: int = 10,
    ) -> tuple[dict[str, Any], list[Any], bool]:
        """Mirror of ShopifyClient.paginate() — calls self.execute() in a loop
        so scripted FakeClient responses are consumed in page order."""
        all_nodes: list[Any] = []
        first_response: dict[str, Any] = {}
        cursor: str | None = None
        for page in range(max_pages):
            page_vars: dict[str, Any] = {**variables, "first": page_size, "after": cursor}
            result = self.execute(query_str, page_vars)
            if page == 0:
                first_response = result
            connection: Any = result
            for key in connection_path:
                connection = (connection or {}).get(key) or {}
            all_nodes.extend(list(connection.get("nodes") or []))
            page_info: dict[str, Any] = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return first_response, all_nodes, False
            cursor = page_info.get("endCursor")
            if cursor is None:
                break
        return first_response, all_nodes, True
