"""
Live parity check: does our product-status vocabulary still match Shopify's?

Story 10.74. The offline suite CANNOT answer this. `FakeClient` returns canned
dicts and the real transport is built with `fetch_schema_from_transport=False`,
so no offline test makes contact with Shopify's schema — the whole reason
`PRODUCT_STATUS_VALUES` could sit three-valued against a four-value enum for
the module's entire life without a single failing test. The offline drop-guard
in tests/unit/tools/test_products.py catches a value being REMOVED from our
tuple; only this runner catches one being ADDED to Shopify's.

READ-ONLY. Introspection and one filtered read. No mutation — deciding whether
Shopify's business layer accepts a given status on write is deliberately NOT
tested here, because that would mean writing to a live store from a test.

Not run by CI (tests/conftest.py holds tests/live back from collection); it
needs SHOPIFY_STORE_URL + SHOPIFY_ACCESS_TOKEN. Run it by hand when touching
the status vocabulary, or when bumping SHOPIFY_API_VERSION:

  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/live/test_product_status_parity.py -v
"""

import pytest

from shopify_mcp.client import ShopifyClient
from shopify_mcp.shopify.operations import products as ops
from shopify_mcp.tools.products import PRODUCT_STATUS_VALUES

PRODUCT_STATUS_ENUM = """
query {
  __type(name: "ProductStatus") {
    enumValues(includeDeprecated: true) { name isDeprecated }
  }
}
"""


@pytest.fixture(scope="module")
def client():
    return ShopifyClient()


def test_vocabulary_matches_the_live_product_status_enum(client):
    """The check that actually contacts Shopify.

    Fails loudly when Shopify adds a status, which is the event that silently
    put this repo a value behind for years. On a failure: add the value to
    PRODUCT_STATUS_VALUES and to ops.PRODUCT_STATUS_QUERY together (the offline
    sync test enforces the pair), and re-read Shopify's description of it before
    assuming it belongs on the write path — UNLISTED, for one, keeps a product
    purchasable by direct link rather than hiding it.
    """
    data = client.execute(PRODUCT_STATUS_ENUM)
    live = {v["name"] for v in data["__type"]["enumValues"] if not v["isDeprecated"]}
    assert live == set(PRODUCT_STATUS_VALUES), (
        f"Shopify's ProductStatus is {sorted(live)}; "
        f"ours is {sorted(PRODUCT_STATUS_VALUES)} — see this module's docstring"
    )


def test_every_status_is_a_usable_filter(client):
    """Each status in the vocabulary narrows the connection without erroring.

    Shopify's search syntax answers an unrecognised field value with an empty
    connection rather than an error, so a status we support but Shopify does not
    would read as a legitimately empty result. Summing the filters against the
    unfiltered total is what distinguishes the two.
    """
    everything, _capped = ops.read_products(client)
    per_status = {}
    for status in PRODUCT_STATUS_VALUES:
        nodes, _c = ops.read_products(client, status=status)
        assert all(n["status"] == status for n in nodes), f"{status} filter leaked other statuses"
        per_status[status] = len(nodes)

    assert sum(per_status.values()) == len(everything), (
        f"filters {per_status} sum to {sum(per_status.values())} "
        f"but the store holds {len(everything)} products — a status is unreachable"
    )
