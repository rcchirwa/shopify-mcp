"""Drive the real write tools and capture the GraphQL they send (Story 9.20).

The schema contract suite's coercion leg (``tests/unit/shopify/
test_admin_schema_contract.py``) needs the payload the code ACTUALLY sends. For
13 documents that payload is built in the tool layer, or executed directly by
``tools/media``, so no operations-layer call can produce it. Each driver here
registers the real tool module on a ``CapturingServer`` + ``FakeClient``,
scripts the reads the tool makes before its write, calls it with
``confirm=True``, and returns every ``execute()`` call it made.

A driver also declares which documents the run MUST emit, and at least how many
times. The contract suite selects captured calls by query TEXT (never by
position: a tool makes several calls per run) and fails a run that emitted less
than it declared, so a driver whose canned reads stop carrying the flow to the
write fails loudly instead of checking nothing.

The canned responses mirror the fixtures in the per-tool suites under
``tests/unit/tools/``. They only need to carry each tool far enough to emit its
write. Tool output is deliberately not asserted here: the per-tool suites own
behaviour, this module owns payload capture.
"""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from shopify_mcp.tools import _write_tool, catalog_hygiene, discounts, inventory, media, products
from shopify_mcp.tools.catalog_hygiene import (
    METAFIELDS_DELETE_MUTATION,
    METAFIELDS_SET_MUTATION,
    PRODUCT_VARIANT_APPEND_MEDIA,
    PRODUCT_VARIANT_DETACH_MEDIA,
    UPDATE_PRODUCT_OPTION,
    UPDATE_PRODUCT_VARIANTS_PRICING,
)
from shopify_mcp.tools.discounts import CREATE_DISCOUNT_CODE_BASIC
from shopify_mcp.tools.inventory import SET_INVENTORY
from shopify_mcp.tools.media import _reorder, _update, _upload
from shopify_mcp.tools.media._graphql import (
    PRODUCT_CREATE_MEDIA,
    PRODUCT_REORDER_MEDIA,
    PRODUCT_UPDATE_MEDIA,
    STAGED_UPLOADS_CREATE,
)
from shopify_mcp.tools.products import UPDATE_PRODUCT_VARIANTS_POLICY
from tests.support.fake_client import CapturingServer, FakeClient

Call = tuple[str, dict[str, Any]]


@dataclass(frozen=True)
class ToolEmitter:
    """``run`` drives one tool invocation; ``must_emit`` maps each document
    text the run must send to the minimum number of times it must send it."""

    run: Callable[[], list[Call]]
    must_emit: dict[str, int]


@contextmanager
def _offline() -> Iterator[None]:
    """Keep a driven write out of the audit log, off the network and off the
    clock. ``log_write`` is patched at each call-site module, since every tool
    module binds its own name for it."""
    with ExitStack() as stack:
        for module in (
            _write_tool,
            catalog_hygiene,
            discounts,
            inventory,
            products,
            _upload,
            _reorder,
            _update,
        ):
            stack.enter_context(patch.object(module, "log_write", lambda *a, **k: None))
        put_ok = type("PutOk", (), {"status_code": 200, "text": ""})()
        stack.enter_context(patch.object(_upload.requests, "put", return_value=put_ok))
        stack.enter_context(patch.object(_upload.time, "sleep"))
        yield


def _drive(
    register: Callable[[Any, Any], None],
    responses: list[Any],
    invoke: Callable[[dict[str, Callable[..., Any]]], Any],
) -> list[Call]:
    server = CapturingServer()
    client = FakeClient(responses)
    register(server, client)
    with _offline():
        invoke(server.tools)
    return [(query, variables or {}) for query, variables in client.calls]


_PRODUCT_GID = "gid://shopify/Product/100"
_VARIANT_A = "gid://shopify/ProductVariant/201"
_VARIANT_B = "gid://shopify/ProductVariant/202"
_MEDIA_1 = "gid://shopify/MediaImage/1"
_MEDIA_2 = "gid://shopify/MediaImage/2"
_MEDIA_3 = "gid://shopify/MediaImage/3"


# ---- tools/catalog_hygiene.py ----------------------------------------------


def _pricing() -> list[Call]:
    read = {
        "product": {
            "id": _PRODUCT_GID,
            "title": "T",
            "variants": {
                "nodes": [
                    {"id": _VARIANT_A, "sku": "SKU-A", "price": "39.99", "compareAtPrice": None}
                ]
            },
        }
    }
    written = {"id": _VARIANT_A, "sku": "SKU-A", "price": "49.99", "compareAtPrice": "59.99"}
    ok = {
        "productVariantsBulkUpdate": {
            "product": {"id": _PRODUCT_GID},
            "productVariants": [written],
            "userErrors": [],
        }
    }
    return _drive(
        catalog_hygiene.register,
        [read, ok],
        lambda tools: tools["update_product_pricing"](
            "100",
            variants=[{"variantId": "SKU-A", "price": "49.99", "compareAtPrice": "59.99"}],
            confirm=True,
        ),
    )


def _binding_read(bound_to_a: list[str]) -> dict[str, Any]:
    return {
        "product": {
            "id": _PRODUCT_GID,
            "title": "T",
            "media": {
                "nodes": [
                    {"id": m, "alt": None, "mediaContentType": "IMAGE", "image": None}
                    for m in (_MEDIA_1, _MEDIA_2, _MEDIA_3)
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
            "variants": {
                "nodes": [
                    {
                        "id": _VARIANT_A,
                        "sku": "SKU-A",
                        "media": {"nodes": [{"id": m} for m in bound_to_a]},
                    },
                    {"id": _VARIANT_B, "sku": "SKU-B", "media": {"nodes": []}},
                ]
            },
        }
    }


def _append_response(user_errors: list[dict[str, Any]]) -> dict[str, Any]:
    return {"productVariantAppendMedia": {"productVariants": [], "userErrors": user_errors}}


_DETACH_OK = {"productVariantDetachMedia": {"product": {"id": _PRODUCT_GID}, "userErrors": []}}


def _binding_detach_then_append() -> list[Call]:
    # Variant A is already bound to MEDIA_1 and asks for [1, 2]: the
    # detach-reattach path. Variant B is unbound: append only.
    return _drive(
        catalog_hygiene.register,
        [_binding_read([_MEDIA_1]), _DETACH_OK, _append_response([])],
        lambda tools: tools["update_variant_image_binding"](
            product_id=_PRODUCT_GID,
            variant_media=[
                {"variantId": _VARIANT_A, "mediaIds": [_MEDIA_1, _MEDIA_2]},
                {"variantId": _VARIANT_B, "mediaIds": [_MEDIA_3]},
            ],
            confirm=True,
        ),
    )


def _binding_append_fails_then_rollback() -> list[Call]:
    # The second productVariantAppendMedia is the rollback built by
    # _handle_append_failure_after_detach, re-binding what the detach removed.
    return _drive(
        catalog_hygiene.register,
        [
            _binding_read([_MEDIA_1]),
            _DETACH_OK,
            _append_response([{"field": [], "message": "Media not found"}]),
            _append_response([]),
        ],
        lambda tools: tools["update_variant_image_binding"](
            product_id=_PRODUCT_GID,
            variant_media=[{"variantId": _VARIANT_A, "mediaIds": [_MEDIA_1, _MEDIA_2]}],
            confirm=True,
        ),
    )


def _set_metafields() -> list[Call]:
    ok: dict[str, Any] = {"metafieldsSet": {"metafields": [], "userErrors": []}}
    return _drive(
        catalog_hygiene.register,
        [ok],
        lambda tools: tools["set_product_metafields"](
            metafields=[
                {
                    "ownerId": _PRODUCT_GID,
                    "namespace": "custom",
                    "key": "fabric_weight_oz",
                    "value": "14",
                    "type": "number_integer",
                }
            ],
            confirm=True,
        ),
    )


def _delete_metafields() -> list[Call]:
    resolved = {
        "e0": {
            "id": "gid://shopify/Metafield/42",
            "namespace": "custom",
            "key": "fabric_weight_oz",
            "ownerType": "PRODUCT",
            "owner": {"id": _PRODUCT_GID},
        }
    }
    ok: dict[str, Any] = {"metafieldsDelete": {"deletedMetafields": [], "userErrors": []}}
    return _drive(
        catalog_hygiene.register,
        [resolved, ok],
        lambda tools: tools["delete_product_metafields"](
            metafields=[{"metafieldId": "gid://shopify/Metafield/42"}], confirm=True
        ),
    )


def _options() -> list[Call]:
    option_gid = "gid://shopify/ProductOption/300"
    value_m = "gid://shopify/ProductOptionValue/4001"
    node = {
        "id": _PRODUCT_GID,
        "title": "T",
        "options": [
            {"id": option_gid, "name": "Size", "optionValues": [{"id": value_m, "name": "M-CRM"}]}
        ],
        "variants": {
            "nodes": [
                {
                    "id": _VARIANT_A,
                    "title": "M-CRM",
                    "selectedOptions": [{"name": "Size", "value": "M-CRM"}],
                }
            ]
        },
    }
    ok = {"productOptionUpdate": {"product": node, "userErrors": []}}
    return _drive(
        catalog_hygiene.register,
        [{"product": node}, ok],
        lambda tools: tools["update_product_options"](
            "100",
            option={"id": option_gid, "name": "Fit"},
            option_values_to_update=[{"id": value_m, "name": "Medium"}],
            variant_strategy="LEAVE_AS_IS",
            confirm=True,
        ),
    )


# ---- tools/discounts.py, tools/inventory.py, tools/products.py ---------------


def _discount() -> list[Call]:
    ok = {
        "discountCodeBasicCreate": {
            "codeDiscountNode": {"id": "gid://shopify/DiscountCodeNode/5001"},
            "userErrors": [],
        }
    }
    # usage_limit and ends_at populate the two optional keys the tool adds.
    return _drive(
        discounts.register,
        [ok],
        lambda tools: tools["create_discount_code"](
            "Fest", "FEST20", 20, usage_limit=5, ends_at="2099-01-01", confirm=True
        ),
    )


def _level(available: int) -> dict[str, Any]:
    return {
        "quantities": [{"name": "available", "quantity": available}],
        "location": {"id": "gid://shopify/Location/9", "name": "Main"},
    }


_SET_INVENTORY_OK: dict[str, Any] = {"inventorySetOnHandQuantities": {"userErrors": []}}


def _update_inventory() -> list[Call]:
    read = {"inventoryItem": {"inventoryLevels": {"nodes": [_level(3)]}}}
    return _drive(
        inventory.register,
        [read, _SET_INVENTORY_OK],
        lambda tools: tools["update_inventory"]("1", "9", 7, confirm=True),
    )


def _update_variant_inventory_quantity() -> list[Call]:
    read = {
        "product": {
            "title": "T",
            "variants": {
                "nodes": [
                    {
                        "id": _VARIANT_A,
                        "title": "S",
                        "sku": "SKU-A",
                        "inventoryItem": {
                            "id": "gid://shopify/InventoryItem/201",
                            "tracked": True,
                            "inventoryLevels": {"nodes": [_level(3)]},
                        },
                    }
                ]
            },
        }
    }
    return _drive(
        inventory.register,
        [read, _SET_INVENTORY_OK],
        lambda tools: tools["update_variant_inventory_quantity"]("100", 7, confirm=True),
    )


def _variant_inventory_policy() -> list[Call]:
    read = {
        "product": {
            "id": _PRODUCT_GID,
            "title": "T",
            "variants": {
                "nodes": [{"id": _VARIANT_A, "title": "S", "inventoryPolicy": "CONTINUE"}]
            },
        }
    }
    ok = {
        "productVariantsBulkUpdate": {
            "product": {"id": _PRODUCT_GID},
            "productVariants": [{"id": _VARIANT_A, "inventoryPolicy": "DENY"}],
            "userErrors": [],
        }
    }
    return _drive(
        products.register,
        [read, ok],
        lambda tools: tools["update_variant_inventory_policy"]("100", "DENY", confirm=True),
    )


# ---- tools/media -------------------------------------------------------------


def _media_read(*media_ids: str) -> dict[str, Any]:
    return {
        "product": {
            "id": _PRODUCT_GID,
            "title": "T",
            "media": {
                "nodes": [
                    {"id": m, "alt": "old", "mediaContentType": "IMAGE", "status": "READY"}
                    for m in media_ids
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
    }


_REORDER_OK = {
    "productReorderMedia": {
        "job": {"id": "gid://shopify/Job/1", "done": True},
        "mediaUserErrors": [],
        "userErrors": [],
    }
}


def _upload_at_featured_position() -> list[Call]:
    # position=1 on a product that already has media: stage, attach, poll,
    # then the upload flow's own productReorderMedia.
    staged = {
        "stagedUploadsCreate": {
            "stagedTargets": [
                {
                    "url": "https://staged.example/signed",
                    "resourceUrl": "https://cdn.example/resource",
                    "parameters": [{"name": "content_type", "value": "image/jpeg"}],
                }
            ],
            "userErrors": [],
        }
    }
    attached = {
        "productCreateMedia": {
            "media": [{"id": _MEDIA_3, "status": "PROCESSING"}],
            "mediaUserErrors": [],
        }
    }
    ready = {"node": {"id": _MEDIA_3, "status": "READY", "preview": None}}
    return _drive(
        media.register,
        [_media_read(_MEDIA_1, _MEDIA_2), staged, attached, ready, _REORDER_OK],
        lambda tools: tools["upload_product_image"](
            "100", "https://cdn.example.com/hero.jpg", alt="Hero", position=1, confirm=True
        ),
    )


def _reorder_media() -> list[Call]:
    return _drive(
        media.register,
        [_media_read(_MEDIA_1, _MEDIA_2), _REORDER_OK],
        lambda tools: tools["reorder_product_media"](
            "100", moves=[{"id": _MEDIA_2, "newPosition": 1}], confirm=True
        ),
    )


def _update_media() -> list[Call]:
    ok: dict[str, Any] = {"productUpdateMedia": {"media": [], "mediaUserErrors": []}}
    return _drive(
        media.register,
        [_media_read(_MEDIA_1), ok],
        lambda tools: tools["update_product_media"]("100", _MEDIA_1, "New alt", confirm=True),
    )


TOOL_EMITTERS: dict[str, ToolEmitter] = {
    "update_product_pricing": ToolEmitter(_pricing, {UPDATE_PRODUCT_VARIANTS_PRICING: 1}),
    "update_variant_image_binding": ToolEmitter(
        _binding_detach_then_append,
        {PRODUCT_VARIANT_DETACH_MEDIA: 1, PRODUCT_VARIANT_APPEND_MEDIA: 1},
    ),
    "update_variant_image_binding[rollback]": ToolEmitter(
        _binding_append_fails_then_rollback, {PRODUCT_VARIANT_APPEND_MEDIA: 2}
    ),
    "set_product_metafields": ToolEmitter(_set_metafields, {METAFIELDS_SET_MUTATION: 1}),
    "delete_product_metafields": ToolEmitter(_delete_metafields, {METAFIELDS_DELETE_MUTATION: 1}),
    "update_product_options": ToolEmitter(_options, {UPDATE_PRODUCT_OPTION: 1}),
    "create_discount_code": ToolEmitter(_discount, {CREATE_DISCOUNT_CODE_BASIC: 1}),
    "update_inventory": ToolEmitter(_update_inventory, {SET_INVENTORY: 1}),
    "update_variant_inventory_quantity": ToolEmitter(
        _update_variant_inventory_quantity, {SET_INVENTORY: 1}
    ),
    "update_variant_inventory_policy": ToolEmitter(
        _variant_inventory_policy, {UPDATE_PRODUCT_VARIANTS_POLICY: 1}
    ),
    "upload_product_image": ToolEmitter(
        _upload_at_featured_position,
        {STAGED_UPLOADS_CREATE: 1, PRODUCT_CREATE_MEDIA: 1, PRODUCT_REORDER_MEDIA: 1},
    ),
    "reorder_product_media": ToolEmitter(_reorder_media, {PRODUCT_REORDER_MEDIA: 1}),
    "update_product_media": ToolEmitter(_update_media, {PRODUCT_UPDATE_MEDIA: 1}),
}
