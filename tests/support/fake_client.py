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

from shopify_mcp.client import ShopifyClient
from shopify_mcp.settings import Settings
from shopify_mcp.shopify._cache import ShopifyMetadataCache


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

    def __init__(
        self,
        responses: Iterable[Any],
        settings: Settings | None = None,
        fetch_results: Iterable[Any] | None = None,
        metadata_cache: ShopifyMetadataCache | None = None,
    ) -> None:
        self.responses: list[Any] = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        # Tools that consult client._settings (webhook allowlist, poll_job
        # backoff/timeout) need a real Settings here, not a sentinel.
        self._settings: Settings = settings or _default_test_settings()
        # Mirror ShopifyClient._metadata_cache (A8 / Story 10.32) so cache-aware
        # tools work against the fake. Defaults to a fresh cache off these
        # settings; tests inject one with a controllable clock to drive TTL expiry.
        self._metadata_cache: ShopifyMetadataCache = metadata_cache or ShopifyMetadataCache(
            self._settings
        )
        # Scripted results for fetch_bytes() — the raw-GET seam that media
        # tools now go through (Story 10.24 / A6). Each item is either a
        # (body, content_type) tuple returned to the caller, or a
        # BaseException instance raised in its place. Defaults to a single
        # successful image download so upload happy-path tests don't have to
        # spell it out; tests exercising download failures pass their own.
        self.fetch_results: list[Any] = (
            list(fetch_results) if fetch_results is not None else [(b"fakejpgbytes", "image/jpeg")]
        )
        self.fetch_calls: list[tuple[str, int, bool]] = []

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        self.calls.append((query, variables))
        if not self.responses:
            raise AssertionError("FakeClient: unexpected extra execute() call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def fetch_bytes(
        self, url: str, *, max_size: int, allow_redirects: bool = False
    ) -> tuple[bytes, str]:
        """Mirror of ShopifyClient.fetch_bytes() — returns the next scripted
        (body, content_type) tuple, or raises a scripted exception. Records the
        call so tests can assert the SSRF-relevant args (allow_redirects, cap)."""
        self.fetch_calls.append((url, max_size, allow_redirects))
        if not self.fetch_results:
            raise AssertionError("FakeClient: unexpected extra fetch_bytes() call")
        item = self.fetch_results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    # THE REAL METHOD, not a copy of it (Story 10.78). paginate() touches
    # nothing on `self` but `execute()`, which FakeClient provides, so the
    # scripted responses are consumed in page order exactly as before — and
    # every operations-layer test in the repo now walks pages through the code
    # that actually ships.
    #
    # This replaces a hand-maintained duplicate that had to be brought back
    # into step by hand twice. A copy can only ever be *checked* for drift, and
    # checking it is unreliable in both directions: a structural guard misses a
    # `break` quietly becoming a `return ..., False` (the two abnormal stops
    # differ by a bool, not by a branch), while tripping on a rename or a
    # reformat that changes nothing. Sharing the method makes drift impossible
    # instead of detectable, which is the only version of this that stays true.
    paginate = ShopifyClient.paginate


def products_page(
    nodes: list[Any], *, has_next: bool = False, cursor: str = "CUR"
) -> dict[str, Any]:
    """One page of the outer products connection, in paginate()'s expected shape.

    Shared by the tools-layer and operations-layer product-list suites (Story
    10.72) so the two cannot drift apart on the fixture shape they both assert
    pagination against.
    """
    return {
        "products": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor if has_next else None},
        }
    }


def collection_products_page(
    nodes: list[Any],
    *,
    has_next: bool = False,
    cursor: str = "CUR",
    collection_id: str = "gid://shopify/Collection/999",
    title: str = "Vanish",
    handle: str = "vanish",
) -> dict[str, Any]:
    """One page of the products connection nested inside ``collectionByHandle``.

    The sibling of ``products_page`` for the two collection-scoped reads (Story
    10.76). Their connection lives one level down, at
    ``connection_path=["collectionByHandle", "products"]``, and the collection's
    own ``id``/``title``/``handle`` ride alongside it on every page — which is
    why they are parameters here: a test proving those fields are taken from the
    FIRST page needs two pages that disagree about them.
    """
    return {
        "collectionByHandle": {
            "id": collection_id,
            "title": title,
            "handle": handle,
            "products": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor if has_next else None},
            },
        }
    }
