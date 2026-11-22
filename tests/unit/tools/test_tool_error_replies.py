"""
A Shopify call that fails must come back as a structured text reply (Story 10.118).

Before this story, a Shopify call outside any ``try`` let the exception leave the
tool. FastMCP then showed ``Error executing tool <name>: <str(exc)>`` with
``isError=true``. The convention in ``catalog_hygiene.py`` is a text reply:
``Error reading <what> (<ExcType>): <capped text>`` for a read and
``Error calling <mutationName> (<ExcType>): <capped text>`` for a write.

Every case below feeds a ``FakeClient`` the right number of good responses, then
one exception, so the raise lands at the call under test. For each Shopify call
of each tool in scope the tests check three things:

  (a) a ``ShopifyError`` returns a str and the exact expected text;
  (b) a ``RuntimeError`` does the same;
  (c) a ``ShopifyTransportError`` adds the "may have landed" note on a WRITE
      call only, and never on a READ call (including a read inside a write tool).

The last section runs two tools on a REAL ``FastMCP`` server, because the fake
server cannot show ``isError``, and checks that ``read_boundary`` keeps the
name, description and input schema of the tool it wraps.
"""

import asyncio
import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest, CallToolRequestParams
from pydantic import SecretStr

import shopify_mcp.tools._write_tool as _wt
from shopify_mcp.errors import ShopifyError, ShopifyTransportError
from shopify_mcp.settings import Settings
from shopify_mcp.tools import (
    collections,
    discounts,
    inventory,
    orders,
    products,
    publications,
    webhooks,
)
from shopify_mcp.tools import media as media_pkg
from shopify_mcp.tools._scrub import REFLECT_MAX_LEN
from shopify_mcp.tools._untrusted import INJECTION_REMINDER, wrap
from tests.support import CapturingServer, FakeClient

# ---------- good responses (shapes read from the existing suites) ----------

_PRODUCT = {
    "product": {
        "id": "gid://shopify/Product/123",
        "title": "T",
        "handle": "t",
        "status": "ACTIVE",
        "bodyHtml": "",
        "tags": [],
        "seo": {"title": None, "description": None},
        "variants": {"nodes": []},
    }
}
_POLICY_PRODUCT = {
    "product": {
        "title": "T",
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "title": "S",
                    "inventoryPolicy": "CONTINUE",
                }
            ]
        },
    }
}
_INVENTORY_PRODUCT = {
    "product": {
        "title": "T",
        "variants": {
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "title": "S",
                    "sku": "SKU",
                    "inventoryItem": {
                        "id": "gid://shopify/InventoryItem/1",
                        "tracked": False,
                        "inventoryLevels": {
                            "nodes": [
                                {
                                    "quantities": [
                                        {"name": "available", "quantity": 5},
                                        {"name": "on_hand", "quantity": 5},
                                        {"name": "committed", "quantity": 0},
                                    ],
                                    "location": {"id": "gid://shopify/Location/9", "name": "Main"},
                                }
                            ]
                        },
                    },
                }
            ]
        },
    }
}
_INVENTORY_ITEM = {"inventoryItem": {"id": "gid://shopify/InventoryItem/1", "inventoryLevels": {}}}
_MANUAL_COLLECTION = {
    "collectionByHandle": {
        "id": "gid://shopify/Collection/1",
        "title": "C",
        "handle": "h",
        "descriptionHtml": "",
    }
}
_NO_COLLECTION = {"collectionByHandle": None}
_MEDIA_PRODUCT = {
    "product": {
        "id": "gid://shopify/Product/123",
        "title": "T",
        "media": {"nodes": [{"id": "gid://shopify/MediaImage/1", "alt": "a"}]},
    }
}
_MEDIA_ID = "gid://shopify/MediaImage/1"


def _webhook_settings() -> Settings:
    return Settings(
        shopify_store_url="test.myshopify.com",
        shopify_access_token=SecretStr("shpat_test00000000000000000000000"),
        webhook_allowlist_hosts="example.com",
    )


# ---------- the case tables ----------


@dataclass(frozen=True)
class Step:
    """One Shopify call a tool makes. ``good`` is the response when it must pass."""

    kind: str  # "read" or "write"
    label: str  # the `what` of a read, the mutation name of a write
    good: Any = None


@dataclass(frozen=True)
class Case:
    module: Any
    tool: str
    kwargs: dict[str, Any]
    steps: tuple[Step, ...]
    settings: Settings | None = field(default=None, compare=False)

    @property
    def id(self) -> str:
        return f"{self.module.__name__.rsplit('.', 1)[-1]}.{self.tool}"


def _read(module: Any, tool: str, label: str, **kwargs: Any) -> Case:
    return Case(module, tool, kwargs, (Step("read", label),))


def _write(
    module: Any,
    tool: str,
    mutation: str,
    *reads: Step,
    settings: Settings | None = None,
    **kwargs: Any,
) -> Case:
    steps = (*reads, Step("write", mutation))
    return Case(module, tool, {**kwargs, "confirm": True}, steps, settings)


# Reads: one Shopify call each (a paginated read fails on its first page).
READ_CASES = [
    _read(products, "get_products", "products"),
    _read(products, "get_product", "product", product_id="123"),
    _read(products, "get_products_by_collection", "collection products", collection_handle="c"),
    _read(products, "get_product_description", "product description", product_id="123"),
    _read(products, "get_products_with_descriptions", "product descriptions"),
    Case(
        products,
        "get_products_with_descriptions",
        {"collection_handle": "c"},
        (Step("read", "product descriptions"),),
    ),
    _read(products, "get_product_full", "product", product_id="123"),
    _read(products, "get_product_collections", "product collections", product_id="123"),
    _read(inventory, "get_inventory", "inventory", product_id="123"),
    _read(collections, "get_collection", "collection", handle="h"),
    _read(orders, "get_orders", "orders", limit=1),
    _read(orders, "get_order", "order", order_id="123"),
    _read(webhooks, "list_webhooks", "webhooks"),
    _read(discounts, "get_discount_codes", "discount codes"),
    _read(media_pkg, "list_product_media", "product media", product_id="123"),
]

# Writes: the read before the write (if any) and the write itself.
WRITE_CASES = [
    _write(
        products,
        "update_product_title",
        "productUpdate",
        Step("read", "product", _PRODUCT),
        product_id="123",
        new_title="New",
    ),
    _write(
        products,
        "update_product_description",
        "productUpdate",
        Step("read", "product", _PRODUCT),
        product_id="123",
        new_description="<p>x</p>",
    ),
    _write(
        products,
        "update_product_seo",
        "productUpdate",
        Step("read", "product SEO", _PRODUCT),
        product_id="123",
        new_seo_title="Hello",
    ),
    _write(
        products,
        "update_product_tags",
        "productUpdate",
        Step("read", "product", _PRODUCT),
        product_id="123",
        new_tags=["a"],
        mode="append",
    ),
    _write(
        products,
        "update_product_tags",
        "productUpdate",
        product_id="123",
        new_tags=["a"],
        mode="replace",
    ),
    _write(
        products,
        "update_product_status",
        "productUpdate",
        Step("read", "product", _PRODUCT),
        product_id="123",
        new_status="DRAFT",
    ),
    _write(
        products,
        "update_variant_inventory_policy",
        "productVariantsBulkUpdate",
        Step("read", "product variants", _POLICY_PRODUCT),
        product_id="123",
        new_policy="DENY",
    ),
    _write(
        inventory,
        "update_inventory",
        "inventorySetOnHandQuantities",
        Step("read", "inventory item", _INVENTORY_ITEM),
        inventory_item_id="1",
        location_id="9",
        quantity=3,
    ),
    _write(
        inventory,
        "update_variant_inventory_quantity",
        "inventorySetOnHandQuantities",
        Step("read", "product inventory", _INVENTORY_PRODUCT),
        product_id="123",
        quantity=3,
    ),
    _write(
        collections,
        "update_collection",
        "collectionUpdate",
        Step("read", "collection", _MANUAL_COLLECTION),
        handle="h",
        new_title="T2",
    ),
    _write(
        collections,
        "create_collection",
        "collectionCreate",
        Step("read", "collection", _NO_COLLECTION),
        title="T",
    ),
    _write(
        collections,
        "add_product_to_collection",
        "collectionAddProductsV2",
        Step("read", "collection", _MANUAL_COLLECTION),
        handle="h",
        product_id="123",
    ),
    _write(
        collections,
        "remove_product_from_collection",
        "collectionRemoveProducts",
        Step("read", "collection", _MANUAL_COLLECTION),
        handle="h",
        product_id="123",
    ),
    _write(
        webhooks,
        "register_webhook",
        "webhookSubscriptionCreate",
        settings=_webhook_settings(),
        topic="ORDERS_CREATE",
        endpoint_url="https://example.com/hook",
    ),
    _write(webhooks, "delete_webhook", "webhookSubscriptionDelete", subscription_id="1"),
    _write(
        discounts,
        "create_discount_code",
        "discountCodeBasicCreate",
        title="T",
        code="CODE",
        percentage_off=10,
    ),
    _write(
        media_pkg,
        "delete_product_media",
        "productDeleteMedia",
        Step("read", "product media", _MEDIA_PRODUCT),
        product_id="123",
        media_ids=[_MEDIA_ID],
    ),
    _write(
        media_pkg,
        "reorder_product_media",
        "productReorderMedia",
        Step("read", "product media", _MEDIA_PRODUCT),
        product_id="123",
        moves=[{"id": _MEDIA_ID, "newPosition": 1}],
    ),
    _write(
        media_pkg,
        "update_product_media",
        "productUpdateMedia",
        Step("read", "product media", _MEDIA_PRODUCT),
        product_id="123",
        media_id=_MEDIA_ID,
        alt="new alt",
    ),
]

# update_variant_inventory_tracking: only its read is unguarded. Its write is
# already inside a try (per variant) and reports a FAILED line.
TRACKING_CASE = Case(
    inventory,
    "update_variant_inventory_tracking",
    {"product_id": "123", "tracked": True, "confirm": True},
    (Step("read", "product inventory"),),
)

ALL_CASES = [*READ_CASES, *WRITE_CASES, TRACKING_CASE]


def _calls(cases: list[Case]) -> list[Any]:
    """One pytest param per (case, step), so the raise lands at each call in turn."""
    return [
        pytest.param(case, index, id=f"{case.id}[{index}:{step.kind}]")
        for case in cases
        for index, step in enumerate(case.steps)
    ]


ALL_CALLS = _calls(ALL_CASES)
WRITE_CALLS = [p for p in ALL_CALLS if p.values[0].steps[p.values[1]].kind == "write"]
READ_CALLS = [p for p in ALL_CALLS if p.values[0].steps[p.values[1]].kind == "read"]

# (factory, fenced). A ShopifyError from the client already carries the fence, so
# its reply leads with the reminder; a bare RuntimeError has none.
FENCED = "<UNTRUSTED-DATA>denied</UNTRUSTED-DATA>"
assert wrap("denied") == FENCED


def _shopify_error() -> Exception:
    return ShopifyError(wrap("denied"))


def _runtime_error() -> Exception:
    return RuntimeError("boom")


def _transport_error() -> Exception:
    return ShopifyTransportError(wrap("reset"))


# ---------- harness ----------


def _run(
    case: Case, index: int, exc: Exception, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, FakeClient, list[tuple[str, str]]]:
    """Run the tool with good responses up to step ``index``, then ``exc``."""
    logged: list[tuple[str, str]] = []

    def _record(name: str, description: str) -> None:
        logged.append((name, description))

    for mod in (case.module, _wt):
        if hasattr(mod, "log_write"):
            monkeypatch.setattr(mod, "log_write", _record)
    good = [step.good for step in case.steps[:index]]
    fc = FakeClient([*good, exc], settings=case.settings)
    srv = CapturingServer()
    case.module.register(srv, fc)
    out = srv.tools[case.tool](**case.kwargs)
    # The raise landed at the intended call: every response was consumed.
    assert fc.responses == []
    assert len(fc.calls) == index + 1
    return out, fc, logged


def _expected(step: Step, exc: Exception, *, fenced: bool, landed: bool = False) -> str:
    verb = "reading" if step.kind == "read" else "calling"
    text = f"Error {verb} {step.label} ({type(exc).__name__}): {str(exc)[:REFLECT_MAX_LEN]}"
    if landed:
        from shopify_mcp.tools._errors import MAY_HAVE_LANDED

        text += "\n" + MAY_HAVE_LANDED
    return (INJECTION_REMINDER if fenced else "") + text


# ---------- (a) and (b): the exact reply, for each call of each tool ----------


@pytest.mark.parametrize(("case", "index"), ALL_CALLS)
@pytest.mark.parametrize(
    ("make_exc", "fenced"),
    [(_shopify_error, True), (_runtime_error, False)],
    ids=["ShopifyError", "RuntimeError"],
)
def test_failed_shopify_call_returns_the_structured_reply(
    case: Case, index: int, make_exc: Any, fenced: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = make_exc()
    out, _fc, logged = _run(case, index, exc, monkeypatch)
    step = case.steps[index]
    assert isinstance(out, str)
    assert out == _expected(step, exc, fenced=fenced)
    assert "may have landed" not in out
    # A failed call writes nothing to the audit log.
    assert logged == []


# ---------- (c): the "may have landed" note ----------


@pytest.mark.parametrize(("case", "index"), WRITE_CALLS)
def test_transport_error_on_a_write_says_the_write_may_have_landed(
    case: Case, index: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = _transport_error()
    out, _fc, logged = _run(case, index, exc, monkeypatch)
    from shopify_mcp.tools._errors import MAY_HAVE_LANDED

    assert MAY_HAVE_LANDED in out
    assert out == _expected(case.steps[index], exc, fenced=True, landed=True)
    assert logged == []


@pytest.mark.parametrize(("case", "index"), READ_CALLS)
def test_transport_error_on_a_read_does_not_say_a_write_may_have_landed(
    case: Case, index: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = _transport_error()
    out, _fc, _logged = _run(case, index, exc, monkeypatch)
    assert out == _expected(case.steps[index], exc, fenced=True)
    assert "may have landed" not in out


def test_the_may_have_landed_text_is_pinned() -> None:
    from shopify_mcp.tools._errors import MAY_HAVE_LANDED

    assert MAY_HAVE_LANDED == (
        "The write may have landed: Shopify can apply a mutation before the connection fails. "
        "Read the item back before you retry."
    )


def test_a_long_message_is_capped_in_both_replies() -> None:
    from shopify_mcp.tools._errors import read_error, write_error

    exc = RuntimeError("x" * 1000)
    assert read_error("things", exc) == "Error reading things (RuntimeError): " + "x" * 300
    assert write_error("m", exc) == "Error calling m (RuntimeError): " + "x" * 300


# ---------- publications: the one unguarded channel read ----------


def test_set_product_publications_guards_the_channel_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_resolve_names`` already loaded the channel cache, so the later
    ``_ensure_channels`` call cannot reach Shopify through the real code. The
    call still sits outside a ``try``, so a failure there must be a reply too."""
    channels = {
        "publications": {
            "nodes": [{"id": "gid://shopify/Publication/1", "name": "Online Store"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    product = {
        "product": {
            "id": "gid://shopify/Product/123",
            "title": "T",
            "handle": "t",
            "status": "ACTIVE",
            "resourcePublicationsV2": {"nodes": []},
        }
    }
    fc = FakeClient([channels, product, product])
    srv = CapturingServer()
    publications.register(srv, fc)
    calls = {"n": 0}
    real = publications._ensure_channels

    def _fail_on_the_second_call(client: Any, cache: dict) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            real(client, cache)
            return
        raise ShopifyError(wrap("denied"))

    monkeypatch.setattr(publications, "_ensure_channels", _fail_on_the_second_call)
    out = srv.tools["set_product_publications"](
        product_id="123", channel_names=["Online Store"], confirm=True
    )
    assert out == (INJECTION_REMINDER + f"Error reading sales channels (ShopifyError): {FENCED}"), (
        out
    )


# ---------- a real FastMCP server ----------


def _call(srv: FastMCP, name: str, arguments: dict[str, Any]) -> Any:
    handler = srv._mcp_server.request_handlers[CallToolRequest]
    request = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments)
    )
    return asyncio.run(handler(request)).root


def test_a_failed_read_is_a_normal_reply_on_a_real_server() -> None:
    srv = FastMCP("t")
    products.register(srv, FakeClient([RuntimeError("boom")]))
    result = _call(srv, "get_product", {"product_id": "123"})
    assert result.isError is False
    assert [c.text for c in result.content] == ["Error reading product (RuntimeError): boom"]


def test_a_failed_write_is_a_normal_reply_on_a_real_server() -> None:
    srv = FastMCP("t")
    products.register(srv, FakeClient([_PRODUCT, RuntimeError("boom")]))
    result = _call(
        srv, "update_product_title", {"product_id": "123", "new_title": "New", "confirm": True}
    )
    assert result.isError is False
    assert [c.text for c in result.content] == ["Error calling productUpdate (RuntimeError): boom"]


# ---------- read_boundary keeps the tool's identity ----------


def _registered(module: Any) -> dict[str, Any]:
    srv = FastMCP("t")
    module.register(srv, FakeClient([]))
    return {t.name: t for t in srv._tool_manager.list_tools()}


def _listed(srv: FastMCP) -> dict[str, Any]:
    return {t.name: t for t in asyncio.run(srv.list_tools())}


READ_TOOL_NAMES = {case.tool for case in READ_CASES}
MODULES = [products, inventory, collections, orders, webhooks, discounts, media_pkg, publications]


def test_read_boundary_wraps_every_unguarded_read_tool_and_no_write_tool() -> None:
    wrapped: set[str] = set()
    for module in MODULES:
        for name, tool in _registered(module).items():
            if inspect.unwrap(tool.fn) is not tool.fn:
                wrapped.add(name)
                # A write tool must never use the read boundary.
                assert tool.annotations is not None
                assert tool.annotations.readOnlyHint is True, name
    assert wrapped == READ_TOOL_NAMES


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_read_boundary_keeps_name_description_and_input_schema(module: Any) -> None:
    decorated = FastMCP("decorated")
    module.register(decorated, FakeClient([]))
    listed = _listed(decorated)
    for name, tool in _registered(module).items():
        raw = inspect.unwrap(tool.fn)
        if raw is tool.fn:
            continue
        plain = FastMCP("plain")
        plain.tool(annotations=tool.annotations)(raw)
        expected = _listed(plain)[name]
        assert listed[name].name == expected.name
        assert listed[name].description == expected.description
        assert listed[name].inputSchema == expected.inputSchema


def test_the_errors_module_is_importable() -> None:
    assert importlib.import_module("shopify_mcp.tools._errors")
