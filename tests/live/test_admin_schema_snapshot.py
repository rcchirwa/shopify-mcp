"""
Live refresh runner for the Admin API schema snapshot (Story 9.16).

``tests/unit/shopify/admin_schema_snapshot.graphql`` is what the offline
contract suite checks every operation against. This runner is the only thing
that compares it with Shopify: it introspects the live store, trims the result
to what the package's documents reach (the same discovery and generator the
contract suite uses), and asserts the committed snapshot is byte-identical.

READ-ONLY. One trivial query and one introspection query. No mutation.

The SERVED ``X-Shopify-Api-Version`` is asserted equal to the pinned version
before anything is compared or written. Shopify answers an unsupported version
with HTTP 200 and a substituted one, so an unasserted introspection gives a
confident, wrong snapshot.

Not run by CI (tests/conftest.py holds tests/live back from collection). It
needs SHOPIFY_STORE_URL + SHOPIFY_ACCESS_TOKEN, and SHOPIFY_API_VERSION is read
from ``.env`` (the process environment does not override it). See README,
"Refreshing the Admin API schema snapshot":

  pytest tests/live/test_admin_schema_snapshot.py -v                                     # check
  REFRESH_ADMIN_SCHEMA_SNAPSHOT=1 pytest tests/live/test_admin_schema_snapshot.py -v     # rewrite

A refresh re-dates the snapshot, which makes every entry in the contract
suite's ``_SERVED_DESPITE_DRIFT`` stale until someone re-confirms it live. That
is intentional: a refresh is the only moment such a fact can change.
"""

import os
from datetime import date

import pytest
from graphql import build_client_schema, get_introspection_query

from shopify_mcp.client import ShopifyClient
from shopify_mcp.settings import Settings
from tests.support.admin_schema_snapshot import (
    INTROSPECTION_OPTIONS,
    SNAPSHOT_PATH,
    all_documents,
    generate_sdl,
    read_snapshot_header,
    render_snapshot,
    snapshot_body,
)

_REFRESH = os.environ.get("REFRESH_ADMIN_SCHEMA_SNAPSHOT") == "1"


def _served_version(client: ShopifyClient) -> str | None:
    return client._transport.response_headers.get("X-Shopify-Api-Version")


@pytest.fixture(scope="module")
def live():
    client = ShopifyClient()  # loads .env before Settings() reads it
    pinned = Settings().shopify_api_version
    client.execute("{ shop { id } }")
    assert _served_version(client) == pinned, (
        f"requested {pinned} but Shopify served {_served_version(client)}; "
        "refusing to compare or write a snapshot of a substituted version"
    )
    data = client.execute(get_introspection_query(**INTROSPECTION_OPTIONS))
    assert _served_version(client) == pinned
    body = generate_sdl(build_client_schema(data), all_documents().values())
    return pinned, body


def test_committed_snapshot_matches_a_fresh_introspection(live):
    served, body = live
    if _REFRESH:
        SNAPSHOT_PATH.write_text(
            render_snapshot(served, date.today().isoformat(), body), encoding="utf-8"
        )
        pytest.skip(f"wrote {SNAPSHOT_PATH} for {served}; review the diff and commit it")

    committed = SNAPSHOT_PATH.read_text(encoding="utf-8")
    committed_version, _captured = read_snapshot_header(committed)
    assert committed_version == served, (
        f"snapshot was captured at {committed_version}, the store now serves {served}"
    )
    assert snapshot_body(committed) == body, (
        "the committed snapshot differs from a fresh trimmed introspection; "
        "refresh with REFRESH_ADMIN_SCHEMA_SNAPSHOT=1 and review the diff"
    )
