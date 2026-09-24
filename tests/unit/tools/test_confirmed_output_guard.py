"""
Guard: no write tool's CONFIRMED output may re-embed its own "PREVIEW — "
header (Story 9.22, following on from Story 9.21's write_gate-level fix of
the same hazard). An operator who reads "Done. ... PREVIEW — ..." (or any
CONFIRMED body that still carries a "PREVIEW" marker) concludes the write
never landed and redoes it by hand.

Story 9.21 fixed this at the write_gate() chokepoint, which structurally
cannot leak (every write_gate() caller with done_text=None returns
_confirmed_from_preview(preview) — proved exhaustively by
tests/unit/tools/test_write_tool.py). That fix could not see the two tools
that build their confirmed output BY HAND outside write_gate
(create_discount_code, and collections._membership_mutation shared by
add_product_to_collection / remove_product_from_collection) — Story 9.22's
defect. This guard is keyed on the TOOLS themselves (enumerated from the
running server), not on write_gate callers, so it can catch the next
hand-built site regardless of which helper (or no helper) it uses.

Design:
  1. `_write_tool_names()` enumerates every registered MCP tool whose
     inputSchema declares a `confirm` parameter, from the actual running
     server (`create_server()` + `list_tools()`) — never hand-listed, so a
     tool added, renamed or removed is picked up automatically.
  2. `_TOOL_CHECKS` maps every one of those tool names to a zero-arg
     function that drives its CONFIRMED (confirm=True, success) path and
     returns the output string. Each check reuses that tool's OWN existing
     test module's fixture-builder functions (imported, not duplicated) —
     the same canned FakeClient responses its own passing tests already use
     — so there is no new, unverified fixture data here.
  3. `test_tool_checks_cover_exactly_the_enumerated_write_tools` asserts the
     enumerated set and `_TOOL_CHECKS`' keys are EXACTLY equal — a tool added
     without a corresponding check (or renamed/removed) fails HERE, loudly,
     rather than the guard below silently iterating over a stale dict (M9).
  4. `test_all_covered_write_tools_confirmed_output_has_no_preview_leak`
     drives every one of the 33 confirmed paths and asserts none contain
     "PREVIEW".
"""

import asyncio
from collections.abc import Callable
from unittest.mock import patch

import pytest

from shopify_mcp.server import create_server

# ---- catalog_hygiene.py ----
from tests.unit.tools.test_catalog_hygiene import (
    _OPT_GID,
    _OV_L,
    _OV_M,
    _S96_MEDIA_1,
    _S96_MEDIA_2,
    _S96_PRODUCT_GID,
    _S96_VARIANT_A,
    _S910_METAFIELD_GID,
    _S910_PRODUCT_GID,
    _options_mutation_ok,
    _options_read_response,
    _product_read_response,
    _product_update_ok,
    _s96_combined_response,
    _s96_mutation_response,
    _s97_entry,
    _s97_mutation_response,
    _s910_batch_resolve,
    _s910_delete_response,
    _s910_gid_alias,
    _taxonomy_response,
    _type_read,
    _type_update_ok,
    _vendor_read,
)
from tests.unit.tools.test_catalog_hygiene import _build as _build_hygiene
from tests.unit.tools.test_catalog_hygiene import _mutation_ok as _hygiene_pricing_mutation_ok
from tests.unit.tools.test_catalog_hygiene import _read_response as _hygiene_pricing_read_response
from tests.unit.tools.test_catalog_hygiene import _update_ok as _hygiene_vendor_update_ok

# ---- collections.py ----
from tests.unit.tools.test_collections import _add_ok as _collections_add_ok
from tests.unit.tools.test_collections import _build as _build_collections
from tests.unit.tools.test_collections import _collection as _collections_collection
from tests.unit.tools.test_collections import _manual_collection
from tests.unit.tools.test_collections import _remove_ok as _collections_remove_ok

# ---- discounts.py ----
from tests.unit.tools.test_discounts import _build as _build_discounts
from tests.unit.tools.test_discounts import _discount_create_ok

# ---- inventory.py ----
from tests.unit.tools.test_inventory import _build as _build_inventory
from tests.unit.tools.test_inventory import (
    _inventory_item_response,
    _set_inventory_ok,
    _tracked_update_ok,
)
from tests.unit.tools.test_inventory import _level as _inv_level
from tests.unit.tools.test_inventory import _product_with_variants as _inv_product_with_variants
from tests.unit.tools.test_inventory import _variant as _inv_variant

# ---- media/*.py ----
from tests.unit.tools.test_media import (
    MEDIA_A,
    MEDIA_B,
    MEDIA_C,
    PRODUCT_GID,
    _create_media_ok,
    _media_node,
    _node_media_status,
    _product_media_read,
    _reorder_ok,
    _staged_ok,
)
from tests.unit.tools.test_media import FakeHTTPResponse as _MediaFakeHTTPResponse
from tests.unit.tools.test_media import _build as _build_media

# ---- products.py ----
from tests.unit.tools.test_products import CUR_HANDLE as _P_CUR_HANDLE
from tests.unit.tools.test_products import CUR_TITLE as _P_CUR_TITLE
from tests.unit.tools.test_products import PROD_ID as _P_PROD_ID
from tests.unit.tools.test_products import _build as _build_products
from tests.unit.tools.test_products import (
    _bulk_policy_ok,
    _product_read,
    _seo_read,
    _status_update_ok,
    _tags_update_ok,
    _variant_policy,
    _variants_policy_read,
)
from tests.unit.tools.test_products import _update_ok as _products_update_ok

# ---- publications.py ----
from tests.unit.tools.test_publications import _build as _build_publications
from tests.unit.tools.test_publications import _channels_response as _pub_channels_response
from tests.unit.tools.test_publications import _collection_pubs as _pub_collection_pubs
from tests.unit.tools.test_publications import _product_pubs as _pub_product_pubs
from tests.unit.tools.test_publications import _publish_ok as _pub_publish_ok
from tests.unit.tools.test_publications import _unpublish_ok as _pub_unpublish_ok

# ---- webhooks.py ----
from tests.unit.tools.test_webhooks import _build as _build_webhooks

# ---------------------------------------------------------------------------
# 1. Enumeration — from the server, never hand-listed.
# ---------------------------------------------------------------------------


def _write_tool_names(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Every registered tool name whose inputSchema declares `confirm`.

    create_server() builds a real ShopifyClient, which requires
    SHOPIFY_STORE_URL / SHOPIFY_ACCESS_TOKEN to pass Settings' validators —
    synthetic values are enough: listing tools touches no network (the
    transport is constructed lazily; nothing here calls .execute()).
    """
    monkeypatch.setenv("SHOPIFY_STORE_URL", "test.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_test00000000000000000000000")
    server = create_server()
    tools = asyncio.run(server.list_tools())
    return {t.name for t in tools if "confirm" in (t.inputSchema.get("properties") or {})}


def test_write_tool_enumeration_is_nonempty_and_contains_9_22_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken or over-narrow filter must not let the completeness check
    below pass vacuously (empty or short enumeration)."""
    names = _write_tool_names(monkeypatch)
    assert len(names) >= 30, names
    assert {
        "create_discount_code",
        "add_product_to_collection",
        "remove_product_from_collection",
    } <= names


# ---------------------------------------------------------------------------
# 2. Per-tool CONFIRMED-path checks — each reuses its own test module's
#    existing fixture-builder functions (imported, not duplicated).
# ---------------------------------------------------------------------------


def _check_create_discount_code() -> str:
    tools, _fc = _build_discounts([_discount_create_ok("5001")])
    return tools["create_discount_code"](
        title="Launch Drop", code="LAUNCH20", percentage_off=20, confirm=True
    )


def _check_add_product_to_collection() -> str:
    tools, _fc = _build_collections([_manual_collection(), _collections_add_ok(job_id="999")])
    return tools["add_product_to_collection"](handle="vanish", product_id="777", confirm=True)


def _check_remove_product_from_collection() -> str:
    tools, _fc = _build_collections([_manual_collection(), _collections_remove_ok(job_id="888")])
    return tools["remove_product_from_collection"](handle="vanish", product_id="777", confirm=True)


def _collections_update_ok() -> dict:
    return {
        "collectionUpdate": {
            "collection": {
                "id": "gid://shopify/Collection/123",
                "title": "new",
                "handle": "vanish",
            },
            "userErrors": [],
        }
    }


def _check_update_collection() -> str:
    tools, _fc = _build_collections(
        [_collections_collection("vanish", "Vanish"), _collections_update_ok()]
    )
    return tools["update_collection"](handle="vanish", new_title="Renamed", confirm=True)


def _collections_create_ok() -> dict:
    return {
        "collectionCreate": {
            "collection": {
                "id": "gid://shopify/Collection/9",
                "title": "Grey Casualty",
                "handle": "grey-casualty",
            },
            "userErrors": [],
        }
    }


def _check_create_collection() -> str:
    tools, _fc = _build_collections([{"collectionByHandle": None}, _collections_create_ok()])
    return tools["create_collection"](title="Grey Casualty", confirm=True)


def _check_update_product_title() -> str:
    tools, _fc = _build_products(
        [
            _product_read(_P_PROD_ID, _P_CUR_TITLE, _P_CUR_HANDLE),
            _products_update_ok(pid=_P_PROD_ID),
        ]
    )
    return tools["update_product_title"](
        product_id=_P_PROD_ID,
        new_title="Totally Different Title",
        change_handle=False,
        confirm=True,
    )


def _check_update_product_description() -> str:
    tools, _fc = _build_products(
        [
            _product_read(_P_PROD_ID, _P_CUR_TITLE, _P_CUR_HANDLE),
            _products_update_ok(pid=_P_PROD_ID),
        ]
    )
    return tools["update_product_description"](
        product_id=_P_PROD_ID,
        new_description="<p>New description.</p>",
        confirm=True,
    )


def _check_update_product_seo() -> str:
    tools, _fc = _build_products([_seo_read(), _products_update_ok(pid="6803111739545")])
    return tools["update_product_seo"](
        product_id="6803111739545",
        new_seo_title="Vanish Trucker Hat | Streetwear",
        new_seo_description="The signature V, embroidered front and center.",
        confirm=True,
    )


def _check_update_product_tags() -> str:
    tools, _fc = _build_products([_tags_update_ok(tags=["vaulted"])])
    return tools["update_product_tags"](
        product_id="123", new_tags=["vaulted"], mode="replace", confirm=True
    )


def _check_update_product_status() -> str:
    tools, _fc = _build_products(
        [_product_read("123", "T", "t"), _status_update_ok(status="ARCHIVED")]
    )
    return tools["update_product_status"](product_id="123", new_status="ARCHIVED", confirm=True)


def _check_update_variant_inventory_policy() -> str:
    variants = [
        _variant_policy("10", "S", "CONTINUE"),
        _variant_policy("11", "M", "CONTINUE"),
        _variant_policy("12", "L", "CONTINUE"),
    ]
    updated = [{"id": v["id"], "inventoryPolicy": "DENY"} for v in variants]
    tools, _fc = _build_products([_variants_policy_read(variants), _bulk_policy_ok(updated)])
    return tools["update_variant_inventory_policy"](
        product_id="123", new_policy="DENY", confirm=True
    )


def _check_register_webhook() -> str:
    tools, _fc = _build_webhooks(
        [
            {
                "webhookSubscriptionCreate": {
                    "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/555"},
                    "userErrors": [],
                }
            }
        ]
    )
    return tools["register_webhook"](
        topic="ORDERS_CREATE", endpoint_url="https://example.com/hook", confirm=True
    )


def _check_delete_webhook() -> str:
    tools, _fc = _build_webhooks(
        [
            {
                "webhookSubscriptionDelete": {
                    "deletedWebhookSubscriptionId": "gid://shopify/WebhookSubscription/123",
                    "userErrors": [],
                }
            }
        ]
    )
    return tools["delete_webhook"](subscription_id="123", confirm=True)


def _check_update_inventory() -> str:
    tools, _fc = _build_inventory(
        [
            _inventory_item_response(available=5),
            {
                "inventorySetOnHandQuantities": {
                    "inventoryAdjustmentGroup": {"createdAt": "2026-04-22T00:00:00Z"},
                    "userErrors": [],
                }
            },
        ]
    )
    return tools["update_inventory"](
        inventory_item_id="42", location_id="9", quantity=0, confirm=True
    )


def _check_update_variant_inventory_tracking() -> str:
    variants = [
        _inv_variant("100", "S", "REEF-S", [], tracked=False),
        _inv_variant("101", "M", "REEF-M", [], tracked=False),
    ]
    tools, _fc = _build_inventory(
        [
            _inv_product_with_variants(variants),
            _tracked_update_ok("gid://shopify/InventoryItem/100", True),
            _tracked_update_ok("gid://shopify/InventoryItem/101", True),
        ]
    )
    return tools["update_variant_inventory_tracking"](product_id="555", tracked=True, confirm=True)


def _check_update_variant_inventory_quantity() -> str:
    variants = [
        _inv_variant("100", "S", "REEF-S", [_inv_level(5, "gid://shopify/Location/9")]),
        _inv_variant("101", "M", "REEF-M", [_inv_level(3, "gid://shopify/Location/9")]),
    ]
    tools, _fc = _build_inventory([_inv_product_with_variants(variants), _set_inventory_ok()])
    return tools["update_variant_inventory_quantity"](product_id="555", quantity=0, confirm=True)


def _check_publish_product_to_channels() -> str:
    tools, _fc = _build_publications(
        [
            _pub_channels_response(),
            _pub_product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
            _pub_publish_ok(),
        ]
    )
    return tools["publish_product_to_channels"](
        product_id="123", channel_names=["Online Store", "Shop"], confirm=True
    )


def _check_unpublish_product_from_channels() -> str:
    tools, _fc = _build_publications(
        [
            _pub_channels_response(),
            _pub_product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3]),
            _pub_unpublish_ok(),
        ]
    )
    return tools["unpublish_product_from_channels"](
        product_id="123", channel_names=["Online Store", "Shop"], confirm=True
    )


def _check_set_product_publications() -> str:
    tools, _fc = _build_publications(
        [
            _pub_channels_response(),
            _pub_product_pubs(pid="123", published_ids=[1, 4], not_published_ids=[2, 3]),
            _pub_publish_ok(),
            _pub_unpublish_ok(),
        ]
    )
    return tools["set_product_publications"](
        product_id="123",
        channel_names=["Point of Sale", "Google & YouTube"],
        confirm=True,
    )


def _check_publish_collection_to_channels() -> str:
    tools, _fc = _build_publications(
        [
            _pub_channels_response(),
            _pub_collection_pubs(published_ids=[1], not_published_ids=[2, 3]),
            _pub_publish_ok(),
        ]
    )
    return tools["publish_collection_to_channels"](
        handle="all-copy", channel_names=["Online Store", "Shop"], confirm=True
    )


def _check_unpublish_collection_from_channels() -> str:
    tools, _fc = _build_publications(
        [
            _pub_channels_response(),
            _pub_collection_pubs(published_ids=[1]),
            _pub_unpublish_ok(),
        ]
    )
    return tools["unpublish_collection_from_channels"](
        handle="all-copy", channel_names=["Online Store"], confirm=True
    )


def _check_upload_product_image() -> str:
    tools, _fc = _build_media(
        [
            _product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B)]),
            _staged_ok(),
            _create_media_ok(mid=MEDIA_C, status="PROCESSING"),
            _node_media_status(
                MEDIA_C, status="READY", preview_url="https://cdn.shopify.com/new.jpg"
            ),
        ]
    )
    with (
        patch(
            "shopify_mcp.tools.media._upload.requests.put",
            return_value=_MediaFakeHTTPResponse(status_code=200),
        ),
        patch("shopify_mcp.tools.media._upload.time.sleep"),
    ):
        return tools["upload_product_image"](
            product_id="123",
            source="https://cdn.example.com/hero.jpg",
            alt="Smoke hero",
            confirm=True,
        )


def _check_reorder_product_media() -> str:
    tools, _fc = _build_media(
        [
            _product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B), _media_node(MEDIA_C)]),
            _reorder_ok(done=True),
        ]
    )
    return tools["reorder_product_media"](
        product_id="123",
        moves=[{"id": MEDIA_C, "newPosition": 1}, {"id": MEDIA_A, "newPosition": 3}],
        confirm=True,
    )


def _check_update_product_media() -> str:
    tools, _fc = _build_media(
        [
            _product_media_read([_media_node(MEDIA_A, alt="")]),
            {
                "productUpdateMedia": {
                    "media": [{"id": MEDIA_A, "alt": "new"}],
                    "mediaUserErrors": [],
                }
            },
        ]
    )
    return tools["update_product_media"](
        product_id="123", media_id=MEDIA_A, alt="new", confirm=True
    )


def _check_delete_product_media() -> str:
    tools, _fc = _build_media(
        [
            _product_media_read([_media_node(MEDIA_A), _media_node(MEDIA_B)]),
            {
                "productDeleteMedia": {
                    "deletedMediaIds": [MEDIA_A],
                    "product": {"id": PRODUCT_GID},
                    "mediaUserErrors": [],
                }
            },
        ]
    )
    return tools["delete_product_media"](product_id="123", media_ids=[MEDIA_A], confirm=True)


def _check_update_product_pricing() -> str:
    tools, _fc = _build_hygiene(
        [
            _hygiene_pricing_read_response(),
            _hygiene_pricing_mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "49.99",
                        "compareAtPrice": "65.00",
                    }
                ]
            ),
        ]
    )
    return tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99", "compareAtPrice": "65.00"}],
        confirm=True,
    )


def _check_update_product_category() -> str:
    tools, _fc = _build_hygiene(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": (
                        "Apparel &amp; Accessories &gt; Clothing &gt; Shirts &amp; Tops &gt; "
                        "Sweatshirts"
                    ),
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    return tools["update_product_category"](
        product_id="5234567890", category="crewneck sweatshirt", confirm=True
    )


def _check_update_product_vendor() -> str:
    tools, _fc = _build_hygiene(
        [
            _vendor_read(pid="5234567890", vendor="Nike"),
            _hygiene_vendor_update_ok(pid="5234567890", vendor="Vanish"),
        ]
    )
    return tools["update_product_vendor"](product_id="5234567890", vendor="Vanish", confirm=True)


def _check_update_product_type() -> str:
    tools, _fc = _build_hygiene(
        [
            _type_read(pid="5234567890", product_type="Old"),
            _type_update_ok(pid="5234567890", product_type="Crewneck Sweatshirt"),
        ]
    )
    return tools["update_product_type"](
        product_id="5234567890", product_type="Crewneck Sweatshirt", confirm=True
    )


def _check_update_variant_image_binding() -> str:
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[
            (
                _S96_VARIANT_A,
                [
                    (_S96_MEDIA_1, "front", "https://cdn/1.jpg"),
                    (_S96_MEDIA_2, "back", None),
                ],
            )
        ]
    )
    tools, _fc = _build_hygiene([combined, mutation])
    return tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )


def _check_set_product_metafields() -> str:
    tools, _fc = _build_hygiene(
        [
            _s97_mutation_response(
                metafields=[
                    {
                        "id": "gid://shopify/Metafield/1",
                        "namespace": "custom",
                        "key": "fabric_weight_oz",
                        "value": "14",
                        "type": "number_integer",
                        "ownerType": "PRODUCT",
                    }
                ]
            )
        ]
    )
    return tools["set_product_metafields"](metafields=[_s97_entry()], confirm=True)


def _check_delete_product_metafields() -> str:
    tools, _fc = _build_hygiene(
        [
            _s910_batch_resolve(_s910_gid_alias()),
            _s910_delete_response(
                deleted=[
                    {
                        "ownerId": _S910_PRODUCT_GID,
                        "namespace": "custom",
                        "key": "fabric_weight_oz",
                    }
                ]
            ),
        ]
    )
    return tools["delete_product_metafields"](
        metafields=[{"metafieldId": _S910_METAFIELD_GID}], confirm=True
    )


def _check_update_product_options() -> str:
    tools, _fc = _build_hygiene(
        [
            _options_read_response(),
            _options_mutation_ok(
                option_name="Sizing",
                values=[{"id": _OV_M, "name": "Medium"}, {"id": _OV_L, "name": "L-CRM"}],
            ),
        ]
    )
    return tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Sizing"},
        option_values_to_update=[{"id": _OV_M, "name": "Medium"}],
        confirm=True,
    )


_TOOL_CHECKS: dict[str, Callable[[], str]] = {
    "create_discount_code": _check_create_discount_code,
    "add_product_to_collection": _check_add_product_to_collection,
    "remove_product_from_collection": _check_remove_product_from_collection,
    "update_collection": _check_update_collection,
    "create_collection": _check_create_collection,
    "update_product_title": _check_update_product_title,
    "update_product_description": _check_update_product_description,
    "update_product_seo": _check_update_product_seo,
    "update_product_tags": _check_update_product_tags,
    "update_product_status": _check_update_product_status,
    "update_variant_inventory_policy": _check_update_variant_inventory_policy,
    "register_webhook": _check_register_webhook,
    "delete_webhook": _check_delete_webhook,
    "update_inventory": _check_update_inventory,
    "update_variant_inventory_tracking": _check_update_variant_inventory_tracking,
    "update_variant_inventory_quantity": _check_update_variant_inventory_quantity,
    "publish_product_to_channels": _check_publish_product_to_channels,
    "unpublish_product_from_channels": _check_unpublish_product_from_channels,
    "set_product_publications": _check_set_product_publications,
    "publish_collection_to_channels": _check_publish_collection_to_channels,
    "unpublish_collection_from_channels": _check_unpublish_collection_from_channels,
    "upload_product_image": _check_upload_product_image,
    "reorder_product_media": _check_reorder_product_media,
    "update_product_media": _check_update_product_media,
    "delete_product_media": _check_delete_product_media,
    "update_product_pricing": _check_update_product_pricing,
    "update_product_category": _check_update_product_category,
    "update_product_vendor": _check_update_product_vendor,
    "update_product_type": _check_update_product_type,
    "update_variant_image_binding": _check_update_variant_image_binding,
    "set_product_metafields": _check_set_product_metafields,
    "delete_product_metafields": _check_delete_product_metafields,
    "update_product_options": _check_update_product_options,
}


# ---------------------------------------------------------------------------
# 3. The guard itself.
# ---------------------------------------------------------------------------


def test_tool_checks_cover_exactly_the_enumerated_write_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ties `_TOOL_CHECKS` to the server's own enumeration: a write tool added
    without a corresponding check fails HERE, loudly, rather than the guard
    below silently iterating over a stale/incomplete dict. A tool removed or
    renamed fails here too (an orphaned check would otherwise KeyError inside
    the loop below with a confusing traceback)."""
    enumerated = _write_tool_names(monkeypatch)
    covered = set(_TOOL_CHECKS)
    assert covered == enumerated, (
        f"missing checks: {enumerated - covered} | stale checks: {covered - enumerated}"
    )


def test_all_covered_write_tools_confirmed_output_has_no_preview_leak() -> None:
    for name, check in _TOOL_CHECKS.items():
        out = check()
        assert "PREVIEW" not in out, f"{name}: leaked PREVIEW in confirmed output: {out!r}"
