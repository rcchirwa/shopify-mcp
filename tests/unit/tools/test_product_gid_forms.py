"""Every tool that builds a Product GID from ``product_id`` accepts both forms
(Story 10.108, T-product-gid-double-prefix).

``to_gid("Product", product_id)`` wrapped whatever it got, so a full GID went
to Shopify as ``gid://shopify/Product/gid://shopify/Product/<n>`` and was
refused. These tests send each affected tool a numeric id, a full Product GID
and a GID of another type, and read what reached the client:

- both good forms send exactly ``gid://shopify/Product/123``;
- the wrong type is refused before any network call.

The client records every call. It answers a call that carries no Product GID
(a sales-channel or collection lookup) from the tool's scripted responses, and
stops the tool at the first call that carries one, because that call is the
evidence.
"""

import contextlib
from collections.abc import Callable
from typing import Any

import pytest

from shopify_mcp.tools import catalog_hygiene, collections, inventory, products, publications
from shopify_mcp.tools._product_resolver import product_id_error
from tests.support import CapturingServer, FakeClient

PRODUCT_PREFIX = "gid://shopify/Product/"
EXPECTED = "gid://shopify/Product/123"


class _Stop(BaseException):
    """Raised at the first Product-carrying call. BaseException, so a tool's
    own ``except Exception`` handler cannot swallow it."""


def _product_gids(value: Any) -> list[str]:
    """Every string in a variables payload that starts like a Product GID."""
    if isinstance(value, str):
        return [value] if value.startswith(PRODUCT_PREFIX) else []
    if isinstance(value, dict):
        return [g for v in value.values() for g in _product_gids(v)]
    if isinstance(value, list):
        return [g for v in value for g in _product_gids(v)]
    return []


class _StopAtProductClient(FakeClient):
    def execute(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        if _product_gids(variables):
            self.calls.append((query, variables))
            raise _Stop
        return super().execute(query, variables)


_COLLECTION = {
    "collectionByHandle": {
        "id": "gid://shopify/Collection/9",
        "title": "C",
        "handle": "c",
        "ruleSet": None,
    }
}

_CHANNELS = {
    "publications": {
        "nodes": [{"id": "gid://shopify/Publication/1", "name": "Online Store"}],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
}

_PUB_ARGS = {"channel_names": ["Online Store"], "confirm": True}

# (module, tool, extra kwargs, responses for the calls before the product read)
_CASES: list[tuple[Any, str, dict[str, Any], list[Any]]] = [
    (products, "get_product", {}, []),
    (products, "get_product_full", {}, []),
    (products, "get_product_description", {}, []),
    (products, "get_product_collections", {}, []),
    (products, "update_product_title", {"new_title": "T"}, []),
    (products, "update_product_description", {"new_description": "D"}, []),
    (products, "update_product_seo", {"new_seo_title": "S"}, []),
    (products, "update_product_tags", {"new_tags": ["a"], "mode": "append"}, []),
    (products, "update_product_status", {"new_status": "DRAFT"}, []),
    (products, "update_variant_inventory_policy", {"new_policy": "DENY"}, []),
    (inventory, "get_inventory", {}, []),
    (inventory, "update_variant_inventory_tracking", {"tracked": True}, []),
    (inventory, "update_variant_inventory_quantity", {"quantity": 0}, []),
    (
        catalog_hygiene,
        "update_product_pricing",
        {"variants": [{"variantId": "1", "price": "10.00"}]},
        [],
    ),
    (collections, "add_product_to_collection", {"handle": "c", "confirm": True}, [_COLLECTION]),
    (
        collections,
        "remove_product_from_collection",
        {"handle": "c", "confirm": True},
        [_COLLECTION],
    ),
    (publications, "get_product_publications", {}, [_CHANNELS]),
    (publications, "publish_product_to_channels", _PUB_ARGS, [_CHANNELS]),
    (publications, "unpublish_product_from_channels", _PUB_ARGS, [_CHANNELS]),
    (publications, "set_product_publications", _PUB_ARGS, [_CHANNELS]),
]
_IDS = [c[1] for c in _CASES]


def _run(module: Any, tool: str, kwargs: dict[str, Any], pre: list[Any], product_id: str):
    srv = CapturingServer()
    fc = _StopAtProductClient(list(pre))
    module.register(srv, fc)
    fn: Callable[..., str] = srv.tools[tool]
    out: str | None = None
    with contextlib.suppress(_Stop):
        out = fn(product_id=product_id, **kwargs)
    return out, fc


@pytest.mark.parametrize(("module", "tool", "kwargs", "pre"), _CASES, ids=_IDS)
@pytest.mark.parametrize("product_id", ["123", EXPECTED], ids=["numeric", "gid"])
def test_both_forms_send_one_single_prefixed_product_gid(module, tool, kwargs, pre, product_id):
    _out, fc = _run(module, tool, kwargs, pre, product_id)
    sent = [g for _q, v in fc.calls for g in _product_gids(v)]
    # The product call must have run, or "nothing double-prefixed" is vacuous.
    assert sent, f"{tool} never sent a Product GID"
    assert set(sent) == {EXPECTED}, sent


@pytest.mark.parametrize(("module", "tool", "kwargs", "pre"), _CASES, ids=_IDS)
@pytest.mark.parametrize(
    "product_id",
    ["gid://shopify/ProductVariant/1", "gid://shopify/Order/123"],
    ids=["variant", "order"],
)
def test_wrong_type_gid_is_refused_before_any_network_call(module, tool, kwargs, pre, product_id):
    out, fc = _run(module, tool, kwargs, pre, product_id)
    assert fc.calls == []
    assert out is not None
    assert "Product GID" in out


@pytest.mark.parametrize("value", [None, 123, "", "123", EXPECTED])
def test_product_id_error_passes_everything_but_a_wrong_type_gid(value):
    # A missing or non-string id is the tool's own concern, not this check's.
    assert product_id_error(value) is None
