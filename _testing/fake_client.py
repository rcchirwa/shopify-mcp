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

    def __init__(self, responses: Iterable[Any]) -> None:
        self.responses: list[Any] = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        self.calls.append((query, variables))
        if not self.responses:
            raise AssertionError("FakeClient: unexpected extra execute() call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
