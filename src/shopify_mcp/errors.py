"""Shopify client error types and the helper that reads their unfenced text.

Story 10.101. These lived in ``shopify_mcp.client``, which imports
``shopify_mcp.tools`` (the header policy, the SSRF guard, the scrub and fence
helpers). A typed operations module that only needed an error class therefore
loaded the whole MCP tools layer, a two-hop path that the AST check in
``tests/architecture/test_layering.py`` cannot see. This module imports nothing
from ``shopify_mcp.client`` or ``shopify_mcp.tools``, so it is safe for the
operations layer. ``shopify_mcp.client`` re-exports every name, so existing
``from shopify_mcp.client import ShopifyError`` imports keep working and give
the identical object.
"""


class _CarriesUnfenced(RuntimeError):
    """Base of the client's two error families: keeps an unfenced copy of the message.

    Story 10.69 (SEC-04-errors). Text Shopify or the transport supplied is
    fenced as untrusted in the message, which is right for a head the model
    reads and wrong for a ```json tail a machine parses. ``unfenced`` is the
    message as it read before the fence existed; :func:`unfenced_message`
    returns it. Text this codebase writes is never fenced, so for it the two
    are equal.
    """

    def __init__(self, message: str, *, unfenced: str | None = None) -> None:
        super().__init__(message)
        self.unfenced = message if unfenced is None else unfenced


class ShopifyError(_CarriesUnfenced):
    """Permanent Shopify failure — do not retry (4xx other than 429,
    schema/permission errors, malformed mutations)."""


class ShopifyProtocolError(ShopifyError):
    """A non-GraphQL response (HTML error page, WAF interstitial, garbage
    200), or no response at all (ShopifyTransportError), that may be
    transient — still a ShopifyError for every existing caller."""


class ShopifyTransportError(ShopifyProtocolError):
    """The request failed in transport (a connection reset, a timeout, a
    broken chunked body). A ShopifyProtocolError, so ``poll_job`` retries it
    to budget; not a TransientShopifyError, so ``execute()`` never retries it,
    because a reset can come after Shopify applied a mutation (Story 10.104)."""


class TransientShopifyError(_CarriesUnfenced):
    """Transient Shopify failure — safe to retry (THROTTLED, 429, 5xx).
    Surfaces to callers only after retries are exhausted."""


def unfenced_message(exc: BaseException) -> str:
    """``str(exc)`` as it read before Story 10.69 fenced it, for a ```json tail.

    A tail keeps raw values (the SEC-04 head/tail split), so it reads this
    rather than the fenced message. Any exception the client did not build is
    its own ``str``. Only the ``execute()`` constructors keep a distinct copy:
    ``fetch_bytes``' errors were fenced by Story 10.95 before this existed,
    have no unfenced form, and no tail reads them. Wire one before a tail does.
    """
    if isinstance(exc, _CarriesUnfenced):
        return exc.unfenced
    return str(exc)  # reflect-ok: every caller caps
