"""Shared HTTP policy for the raw-`requests` stack.

The codebase talks HTTP two ways: the gql `RequestsHTTPTransport` in
`shopify_client.py` (Shopify Admin GraphQL) and direct `requests` calls in
`tools/media/_upload.py` (image download + staged-upload PUT). This module is
the single source of *header* policy for the latter so both stacks present a
consistent identity — see TECH_DEBT N4 ("two HTTP stacks, no shared policy").

Timeout policy lives on `Settings` (`request_timeout_s`,
`staged_upload_timeout_s`); this module owns the header set. Keep both stacks
sourcing their User-Agent from `Settings.http_user_agent` so a change lands in
one place.
"""

from settings import Settings


def default_headers(settings: Settings) -> dict[str, str]:
    """Headers every raw-`requests` call should carry.

    Currently just the shared User-Agent. Returned fresh each call so callers
    can merge in request-specific headers (e.g. signed staged-upload
    parameters) without mutating shared state.
    """
    return {"User-Agent": settings.http_user_agent}
