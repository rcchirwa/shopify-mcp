"""
Story 10.75 (SEC-24-remaining-sites): caller- and Shopify-supplied text that a
tool echoes back is scrubbed with the shared `tools/_scrub` helpers.

Rule, decided on the card 2026-09-28: `cap(sanitize_control_chars(x))`.
Sanitize first so the escapes count toward `REFLECT_MAX_LEN`; ordinary values
render byte-for-byte as before.

- Class A: every "No {kind} found with {identifier}" reply, asserted at EVERY
  site (21), not one per module. Each site is driven with an over-long value, a
  CR/LF value and an ordinary value, and the whole reply is compared.
- Class B: the userError routes that do their own framing around the shared
  joiner (`write_gate`, `create_discount_code`, `update_variant_inventory_policy`)
  and the one joiner that does not use it (`update_product_options`' `_fmt`).

The structural guard against a 22nd unscrubbed site is
`tests/architecture/test_reflection_scrub_guard.py`. CR/LF payloads are built
from escapes, never typed.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_reflection_scrub.py -v
"""

import json
from collections.abc import Callable
from typing import Any

import pytest

from shopify_mcp.shopify._ids import (
    INVALID_INVENTORY_ITEM_ID_ERROR,
    INVALID_LOCATION_ID_ERROR,
    INVALID_ORDER_ID_ERROR,
    INVALID_SUBSCRIPTION_ID_ERROR,
)
from shopify_mcp.tools import catalog_hygiene
from shopify_mcp.tools._scrub import REFLECT_MAX_LEN
from shopify_mcp.tools._untrusted import INJECTION_REMINDER
from tests.support import FakeClient, fenced
from tests.unit.tools.test_catalog_hygiene import (
    _OPT_GID,
    _OV_M,
    _S96_MEDIA_1,
    _S96_VARIANT_A,
    _S911_PRODUCT_GID,
    _options_read_response,
    _parse_tail,
    _read_response,
    _s96_combined_response,
    _s97_entry,
    _s910_batch_resolve,
    _s910_triple_alias,
    _s911_product_response,
)
from tests.unit.tools.test_catalog_hygiene import _build as _build_hygiene
from tests.unit.tools.test_collections import _build as _build_collections
from tests.unit.tools.test_collections import _manual_collection
from tests.unit.tools.test_discounts import _build as _build_discounts
from tests.unit.tools.test_discounts import _discount_create_err
from tests.unit.tools.test_inventory import _build as _build_inventory
from tests.unit.tools.test_inventory import (
    _inventory_item_response,
    _level,
    _product_with_variants,
    _set_inventory_ok,
    _variant,
)
from tests.unit.tools.test_media import MEDIA_A, _media_node, _product_media_read
from tests.unit.tools.test_media import _build as _build_media
from tests.unit.tools.test_orders import _build as _build_orders
from tests.unit.tools.test_products import _build as _build_products
from tests.unit.tools.test_products import (
    _bulk_policy_err,
    _seo_read,
    _update_err,
    _variant_policy,
    _variants_policy_read,
)
from tests.unit.tools.test_webhooks import _build as _build_webhooks

_CR, _LF = "\r", "\n"

# Over-long: 400 chars, capped to REFLECT_MAX_LEN. All digits, so the id-shaped
# tools take the id channel rather than the handle channel.
LONG = "9" * 400
# A value that would forge a second apparent line if echoed raw. GID-shaped so
# `update_variant_image_binding` resolves it on the id channel and echoes it
# verbatim at its id sites; a bare "12\r\n…" would take the handle channel
# there and exercise :1147 instead.
CRLF = "gid://shopify/Product/12" + _CR + _LF + "Injected: yes"
# Ordinary: must render exactly as it did before the sweep (AC 4).
SHORT = "12345"


def _scrubbed(value: str) -> str:
    """The rule, spelled independently of the helpers under test."""
    return value.replace(_CR, "\\r").replace(_LF, "\\n")[:REFLECT_MAX_LEN]


_NF_PRODUCT = [{"product": None}]
_NF_COLLECTION = [{"collectionByHandle": None}]
_ID = "No product found with id {}."
_HANDLE = "No collection found with handle '{}'."


def _call(build: Callable, responses: list, tool: str, arg: str, **kw: Any) -> Callable:
    def run(value: str) -> str:
        tools, _fc = build([dict(r) for r in responses])
        return tools[tool](**{arg: value}, **kw)

    return run


def _image_binding_second_page(value: str) -> str:
    """Page 1 of the product's media says there is more; page 2 finds no product."""
    tools, _fc = _build_hygiene(
        [
            _s96_combined_response(
                [_S96_MEDIA_1],
                [(_S96_VARIANT_A, "sku-a", [])],
                media_has_next_page=True,
                media_end_cursor="c1",
            ),
            {"product": None},
        ]
    )
    return tools["update_variant_image_binding"](
        product_id=value,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
    )


_BINDING = {"variant_media": [{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}]}
_PRICING = {"variants": [{"variantId": "201", "price": "49.99"}]}

# (id, runner, template, renders_json_tail). One row per site; the file:line in
# each id is the site on origin/main e5e2d7a, for traceability only.
CLASS_A_SITES = [
    # products.py
    ("products:391 update_product_seo", _call(_build_products, _NF_PRODUCT, "update_product_seo", "product_id", new_seo_title="T", confirm=True), _ID, False),
    ("products:540 get_products_by_collection", _call(_build_products, _NF_COLLECTION, "get_products_by_collection", "collection_handle"), _HANDLE, False),
    ("products:627 get_products_with_descriptions", _call(_build_products, _NF_COLLECTION, "get_products_with_descriptions", "collection_handle", limit=10), _HANDLE, False),
    ("products:739 get_product_collections", _call(_build_products, _NF_PRODUCT, "get_product_collections", "product_id"), _ID, False),
    ("products:787 update_product_tags", _call(_build_products, _NF_PRODUCT, "update_product_tags", "product_id", mode="append", new_tags=["x"], confirm=True), _ID, False),
    ("products:854 update_product_status", _call(_build_products, _NF_PRODUCT, "update_product_status", "product_id", new_status="ACTIVE", confirm=True), _ID, False),
    ("products:903 update_variant_inventory_policy", _call(_build_products, _NF_PRODUCT, "update_variant_inventory_policy", "product_id", new_policy="DENY", confirm=True), _ID, False),
    # collections.py (:359 is the shared add/remove membership path; both routes)
    ("collections:95 get_collection", _call(_build_collections, _NF_COLLECTION, "get_collection", "handle"), _HANDLE, False),
    ("collections:127 update_collection", _call(_build_collections, _NF_COLLECTION, "update_collection", "handle", new_title="T", confirm=True), _HANDLE, False),
    ("collections:359 add_product_to_collection", _call(_build_collections, _NF_COLLECTION, "add_product_to_collection", "handle", product_id="777", confirm=True), _HANDLE, False),
    ("collections:359 remove_product_from_collection", _call(_build_collections, _NF_COLLECTION, "remove_product_from_collection", "handle", product_id="777", confirm=True), _HANDLE, False),
    # catalog_hygiene.py (these render a head plus a JSON tail)
    ("catalog_hygiene:1689 update_product_pricing", _call(_build_hygiene, _NF_PRODUCT, "update_product_pricing", "product_id", **_PRICING), _ID, True),
    ("catalog_hygiene:2620 update_variant_image_binding", _call(_build_hygiene, _NF_PRODUCT, "update_variant_image_binding", "product_id", **_BINDING), _ID, True),
    ("catalog_hygiene:2640 update_variant_image_binding page 2", _image_binding_second_page, _ID, True),
    # media/
    ("media/_list:75 list_product_media", _call(_build_media, _NF_PRODUCT, "list_product_media", "product_id"), _ID, False),
    ("media/_upload:409 upload_product_image", _call(_build_media, _NF_PRODUCT, "upload_product_image", "product_id", source="https://cdn.example.com/a.jpg", confirm=True), _ID, False),
    ("media/_reorder:60 reorder_product_media", _call(_build_media, _NF_PRODUCT, "reorder_product_media", "product_id", moves=[{"id": MEDIA_A, "newPosition": 1}], confirm=True), _ID, False),
    ("media/_update:44 update_product_media", _call(_build_media, _NF_PRODUCT, "update_product_media", "product_id", media_id=MEDIA_A, alt="x", confirm=True), _ID, False),
    ("media/_delete:43 delete_product_media", _call(_build_media, _NF_PRODUCT, "delete_product_media", "product_id", media_ids=[MEDIA_A], confirm=True), _ID, False),
    # inventory.py
    ("inventory:199 update_variant_inventory_tracking", _call(_build_inventory, _NF_PRODUCT, "update_variant_inventory_tracking", "product_id", tracked=True, confirm=True), _ID, False),
    ("inventory:359 update_variant_inventory_quantity", _call(_build_inventory, _NF_PRODUCT, "update_variant_inventory_quantity", "product_id", quantity=0, confirm=True), _ID, False),
]  # fmt: skip


def _assert_reply(out: str, head: str, tail: bool) -> None:
    if not tail:
        assert out == head
        return
    assert out.startswith(head + "\n\n")
    assert _parse_tail(out)["errors"] == [{"message": head}]


# `update_variant_image_binding` echoes `_identifier_channel`'s display_ref,
# which its producer already bounds to catalog_hygiene's 200-char
# `_GID_DISPLAY_MAX`. Length was never open at these two sites; CR/LF was.
_PRODUCER_BOUND = {
    "catalog_hygiene:2620 update_variant_image_binding": 200,
    "catalog_hygiene:2640 update_variant_image_binding page 2": 200,
}


@pytest.mark.parametrize(
    ("site", "run", "template", "tail"), CLASS_A_SITES, ids=[s[0] for s in CLASS_A_SITES]
)
def test_s1075_not_found_caps_an_over_long_identifier(site, run, template, tail) -> None:
    out = run(LONG)
    _assert_reply(out, template.format("9" * _PRODUCER_BOUND.get(site, REFLECT_MAX_LEN)), tail)
    assert LONG not in out


@pytest.mark.parametrize(
    ("run", "template", "tail"), [s[1:] for s in CLASS_A_SITES], ids=[s[0] for s in CLASS_A_SITES]
)
def test_s1075_not_found_cannot_forge_a_second_line(run, template, tail) -> None:
    out = run(CRLF)
    head = template.format(_scrubbed(CRLF))
    _assert_reply(out, head, tail)
    assert _CR not in head and _LF not in head


@pytest.mark.parametrize(
    ("run", "template", "tail"), [s[1:] for s in CLASS_A_SITES], ids=[s[0] for s in CLASS_A_SITES]
)
def test_s1075_not_found_ordinary_identifier_is_byte_identical(run, template, tail) -> None:
    _assert_reply(run(SHORT), template.format(SHORT), tail)


def test_s1075_class_a_table_matches_the_sites_in_the_source() -> None:
    """Bind the table to the code, not to itself: per module, the rows here
    must cover as many sites as the AST walk finds. The handle precedent,
    catalog_hygiene.py:1147, is tested separately below."""
    from collections import Counter

    from tests.architecture.test_reflection_scrub_guard import _not_found_fstrings

    in_source = Counter(rel.removesuffix(".py") for rel, _line, _node in _not_found_fstrings())
    in_source["catalog_hygiene"] -= 1
    in_table = Counter(site.split(":")[0] for site in {s[0].split()[0] for s in CLASS_A_SITES})
    assert in_table == in_source


# ---------- catalog_hygiene.py:1147 — the precedent site, now on the rule ----------
#
# It keeps its own 200-char `_cap` bound (`_GID_DISPLAY_MAX`) and its `!r`
# quoting; sanitizing first only changes values that carry CR/LF.


def test_s1075_handle_precedent_caps_and_keeps_repr_quoting() -> None:
    fc = FakeClient([{"productByHandle": None}])
    _gid, err = catalog_hygiene._resolve_product_gid(fc, None, handle="h" * 400)
    assert err == f"No product found with handle {'h' * 200!r}."


def test_s1075_handle_precedent_sanitizes_before_repr() -> None:
    fc = FakeClient([{"productByHandle": None}])
    handle = "ghost" + _CR + _LF + "Injected: yes"
    _gid, err = catalog_hygiene._resolve_product_gid(fc, None, handle=handle)
    assert err == f"No product found with handle {_scrubbed(handle)!r}."
    assert _CR not in err and _LF not in err


def test_s1075_handle_precedent_ordinary_handle_is_byte_identical() -> None:
    fc = FakeClient([{"productByHandle": None}])
    _gid, err = catalog_hygiene._resolve_product_gid(fc, None, handle="ghost-handle")
    assert err == "No product found with handle 'ghost-handle'."


# ---------- Class B: routes that frame the shared joiner ----------

_LONG_MSG = "m" * 1000
# Story 10.69: a fenced report keeps REFLECT_MAX_LEN less the 33 delimiter characters.
_ROOM = REFLECT_MAX_LEN - len(fenced(""))


def test_s1075_write_gate_route_caps_the_user_error_text() -> None:
    tools, _fc = _build_products([_seo_read(), _update_err(["product", "seo", "title"], _LONG_MSG)])
    out = tools["update_product_seo"](product_id="123", new_seo_title="x", confirm=True)
    assert out == INJECTION_REMINDER + "Error: " + fenced(
        ("product.seo.title: " + _LONG_MSG)[:_ROOM]
    )


def test_s1075_write_gate_route_escapes_crlf_in_user_error_text() -> None:
    tools, _fc = _build_products(
        [_seo_read(), _update_err(["product", "seo", "title"], "bad" + _LF + "Injected: yes")]
    )
    out = tools["update_product_seo"](product_id="123", new_seo_title="x", confirm=True)
    assert out == INJECTION_REMINDER + "Error: " + fenced("product.seo.title: bad\\nInjected: yes")


def test_s1075_discount_route_caps_the_user_error_text() -> None:
    tools, _fc = _build_discounts([_discount_create_err(["basicCodeDiscount", "code"], _LONG_MSG)])
    out = tools["create_discount_code"](title="T", code="X", percentage_off=10, confirm=True)
    assert out == (
        INJECTION_REMINDER
        + "Error creating discount code: "
        + fenced(("basicCodeDiscount.code: " + _LONG_MSG)[:_ROOM])
    )


def test_s1075_inventory_policy_route_caps_the_user_error_text() -> None:
    tools, _fc = _build_products(
        [
            _variants_policy_read([_variant_policy("10", "S", "CONTINUE")]),
            _bulk_policy_err(["variants", "0", "inventoryPolicy"], _LONG_MSG),
        ]
    )
    out = tools["update_variant_inventory_policy"](
        product_id="123", new_policy="DENY", confirm=True
    )
    assert out == INJECTION_REMINDER + "Error: " + fenced(
        ("variants.0.inventoryPolicy: " + _LONG_MSG)[:_ROOM]
    )


def _option_update_err(user_errors: list[dict[str, Any]]) -> str:
    tools, _fc = _build_hygiene(
        [
            _options_read_response(),
            {"productOptionUpdate": {"product": None, "userErrors": user_errors}},
        ]
    )
    return tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID},
        option_values_to_update=[{"id": _OV_M, "name": "L-CRM"}],
        confirm=True,
    )


# Story 10.69: the report is fenced and the head leads with the reminder.
_OPT_HEAD = INJECTION_REMINDER + "Error: productOptionUpdate userErrors: "


def test_s1075_option_update_joiner_caps_many_errors() -> None:
    errors = [
        {"field": ["optionValuesToUpdate", str(i), "name"], "message": "taken", "code": "TAKEN"}
        for i in range(40)
    ]
    joined = "; ".join(f"optionValuesToUpdate.{i}.name [TAKEN]: taken" for i in range(40))
    out = _option_update_err(errors)
    assert out.startswith(_OPT_HEAD + fenced(joined[:_ROOM]) + "\n\n")


def test_s1075_option_update_joiner_escapes_crlf() -> None:
    out = _option_update_err(
        [
            {
                "field": ["option", "name"],
                "message": "dup" + _CR + _LF + "Injected: yes",
                "code": None,
            }
        ]
    )
    assert out.startswith(_OPT_HEAD + fenced("option.name: dup\\r\\nInjected: yes") + "\n\n")


def test_s1075_option_update_joiner_escapes_count_toward_the_bound() -> None:
    # Sanitize-then-cap: 200 LFs are 400 chars of escapes, still capped at
    # REFLECT_MAX_LEN. Cap-then-sanitize would emit up to twice that.
    out = _option_update_err([{"field": ["option", "name"], "message": _LF * 200, "code": None}])
    expected = ("option.name: " + "\\n" * 200)[:_ROOM]
    assert out.startswith(_OPT_HEAD + fenced(expected) + "\n\n")


def test_s1075_option_update_joiner_ordinary_error_is_byte_identical() -> None:
    out = _option_update_err(
        [
            {
                "field": ["optionValuesToUpdate", "0", "name"],
                "message": "Option value name already exists.",
                "code": "DUPLICATE_OPTION_VALUE_NAME",
            }
        ]
    )
    assert out.startswith(
        _OPT_HEAD
        + fenced(
            "optionValuesToUpdate.0.name [DUPLICATE_OPTION_VALUE_NAME]: "
            "Option value name already exists."
        )
        + "\n\n"
    )


# ---------- Story 10.103: the same echo in other wordings, found by subject ----------
#
# Each row drives one site group with an ordinary value, a CR/LF value and an
# over-long value. The expected reply is the ordinary reply with each echo of
# the ordinary value replaced by the scrubbed payload, so every row compares
# the WHOLE reply (head and parsed JSON tail). `echoes` pins how many times the
# ordinary value appears, so the swap is exact and the payload is proven to
# reach the site. Where an earlier GID rule refuses CR/LF (10.110: digits
# only), the CR/LF row compares the whole refusal instead; length is still open
# there. Site lines are on origin/main 5418d9a, for traceability only.

_INJECT = _CR + _LF + "Injected: yes"
_PID = "gid://shopify/Product/4242"
_ONE_VARIANT = [_variant("100", "S", "REEF-S", [_level(5)])]


class _Row:
    def __init__(
        self,
        site: str,
        run: Callable[[str], str],
        short: str,
        *,
        echoes: int = 1,
        tail: bool = False,
        crlf: str | None = None,
        long: str | None = None,
        refused: str | None = None,
        shown: Callable[[str], str] = lambda v: v,
        tail_raw: bool = False,
    ) -> None:
        self.site, self.run, self.short, self.echoes, self.tail = site, run, short, echoes, tail
        self.crlf = crlf if crlf is not None else short + _INJECT
        self.long = long if long is not None else short + "7" * 400
        self.refused = refused
        # How the site renders the value (e.g. `from_gid` shows only the GID's tail).
        self.shown = shown
        # The JSON tail echoes the normalized input as data. `json.dumps` escapes
        # CR/LF there, so no line is forged; its length is the 10.75 JSON-tail
        # residual that Robert kept OUT (2026-11-05). The tail keeps the raw value.
        self.tail_raw = tail_raw


def _inv(tool: str, responses: list, **kw: Any) -> Callable[[str], str]:
    arg = kw.pop("arg")
    return lambda v: _build_inventory([dict(r) for r in responses])[0][tool](**{arg: v}, **kw)


def _trailing(value: str) -> dict[str, str]:
    """Payloads that survive a strip-then-look-up: CR/LF or spaces after a valid value."""
    return {"crlf": value + _CR + _LF, "long": value + " " * 400}


def _with_tail(out: str) -> tuple[str, Any]:
    """Split a reply into its text (head and any text after the tail) and its parsed tail."""
    head, sep, rest = out.partition("\n\n```json\n")
    assert sep, out
    _json, sep, after = rest.partition("\n```")
    assert sep, out
    return head + "\n\n```json```" + after, _parse_tail(out)


def _swap(obj: Any, old: str, new: str) -> Any:
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, list):
        return [_swap(x, old, new) for x in obj]
    if isinstance(obj, dict):
        return {k: _swap(v, old, new) for k, v in obj.items()}
    return obj


S10103_ROWS = [
    _Row("inventory:121 get_inventory", _inv("get_inventory", [{"product": None}], arg="product_id"), _PID),
    _Row("orders:111 get_order", lambda v: _build_orders([{"order": None}])[0]["get_order"](order_id=v), "4242", refused=f"Error: {INVALID_ORDER_ID_ERROR}."),
    _Row("inventory:195 update_inventory item", _inv("update_inventory", [_inventory_item_response(5, "8")], arg="inventory_item_id", location_id="9", quantity=1), "4242", refused=f"Error: {INVALID_INVENTORY_ITEM_ID_ERROR}."),
    _Row("inventory:196 update_inventory location", _inv("update_inventory", [_inventory_item_response(5, "8")], arg="location_id", inventory_item_id="7", quantity=1), "4242", refused=f"Error: {INVALID_LOCATION_ID_ERROR}."),
    _Row("inventory:469/510 quantity location preview", _inv("update_variant_inventory_quantity", [_product_with_variants(_ONE_VARIANT)], arg="location_id", product_id="555", quantity=0), "4242", echoes=2, refused=f"Error: {INVALID_LOCATION_ID_ERROR}."),
    _Row("inventory:469/552 quantity location confirm", _inv("update_variant_inventory_quantity", [_product_with_variants(_ONE_VARIANT)], arg="location_id", product_id="555", quantity=0, confirm=True), "4242", echoes=2, refused=f"Error: {INVALID_LOCATION_ID_ERROR}."),
    # The written path: the level matches the location. `typed_gid` strips before its
    # digits check, so only trailing whitespace passes it and reaches this echo.
    _Row("inventory:610 quantity location written", _inv("update_variant_inventory_quantity", [_product_with_variants([_variant("100", "S", "REEF-S", [_level(5, "gid://shopify/Location/4242")])]), _set_inventory_ok()], arg="location_id", product_id="555", quantity=0, confirm=True), "gid://shopify/Location/4242", crlf="gid://shopify/Location/4242" + _CR + _LF, long="gid://shopify/Location/4242" + " " * 400),
    _Row("inventory:276 tracking variant ids", lambda v: _build_inventory([_product_with_variants(_ONE_VARIANT)])[0]["update_variant_inventory_tracking"](product_id="555", tracked=True, variant_ids=["100", v]), "4242"),
    _Row("inventory:498 quantity variant ids", lambda v: _build_inventory([_product_with_variants(_ONE_VARIANT)])[0]["update_variant_inventory_quantity"](product_id="555", quantity=0, variant_ids=["100", v]), "4242"),
    _Row("products:951 policy variant ids", lambda v: _build_products([_variants_policy_read([_variant_policy("10", "S", "CONTINUE")])])[0]["update_variant_inventory_policy"](product_id="123", new_policy="DENY", variant_ids=["10", v]), "4242"),
    _Row("products:299 update_product_title preview", lambda v: _build_products([{"product": None}])[0]["update_product_title"](product_id=v, new_title="New"), _PID),
    _Row("products:355 update_product_description preview", lambda v: _build_products([{"product": None}])[0]["update_product_description"](product_id=v, new_description="<p>x</p>"), _PID),
    _Row("media/_update:49 update_product_media", lambda v: _build_media([_product_media_read([_media_node(MEDIA_A)])])[0]["update_product_media"](product_id="123", media_id=v, alt="x"), "gid://shopify/MediaImage/4242"),
    _Row("media/_delete:69 delete_product_media", lambda v: _build_media([_product_media_read([_media_node(MEDIA_A)])])[0]["delete_product_media"](product_id="123", media_ids=[MEDIA_A, v]), "gid://shopify/MediaImage/4242"),
    _Row("collections:235 create_collection", lambda v: _build_collections([])[0]["create_collection"](title="T", handle=v), "!!!", crlf="!!" + _CR + _LF + "!!", long="!" * 403),
    _Row("collections:377 add_product_to_collection preview", lambda v: _build_collections([_manual_collection()])[0]["add_product_to_collection"](handle="vanish", product_id=v), "4242"),
    # catalog_hygiene.py (head plus JSON tail)
    _Row("catalog_hygiene:1903/1905 update_product_pricing", lambda v: _build_hygiene([_read_response()])[0]["update_product_pricing"]("100", variants=[{"variantId": v, "price": "49.99"}]), "gid://shopify/ProductVariant/4242", tail=True, shown=lambda v: v.rsplit("/", 1)[-1]),
    _Row("catalog_hygiene:3268 set_product_metafields preview", lambda v: _build_hygiene([])[0]["set_product_metafields"](metafields=[{**_s97_entry(), "ownerId": v}]), _PID, tail=True, tail_raw=True),
    _Row("catalog_hygiene:3592/3595 delete_product_metafields", lambda v: _build_hygiene([_s910_batch_resolve(None)])[0]["delete_product_metafields"](metafields=[{"ownerId": v, "namespace": "custom", "key": "k"}]), _PID, tail=True),
    _Row("catalog_hygiene:4016 get_product_metafields namespace", lambda v: _build_hygiene([_s911_product_response([])])[0]["get_product_metafields"](product_id=_S911_PRODUCT_GID, namespace=v), "ns4242", tail=True),
    _Row("catalog_hygiene:4019 get_product_metafields keys", lambda v: _build_hygiene([_s911_product_response([])])[0]["get_product_metafields"](product_id=_S911_PRODUCT_GID, keys=[v]), "key4242", tail=True, shown=lambda v: f'"{v}"'),
    # Review round 1: caller metafield namespace / key, unknown reorder ids, webhook id.
    _Row("catalog_hygiene:3273 set_product_metafields namespace", lambda v: _build_hygiene([])[0]["set_product_metafields"](metafields=[{**_s97_entry(), "namespace": v}]), "ns4242", tail=True, tail_raw=True),
    _Row("catalog_hygiene:3273 set_product_metafields key", lambda v: _build_hygiene([])[0]["set_product_metafields"](metafields=[{**_s97_entry(), "key": v}]), "key4242", tail=True, tail_raw=True),
    _Row("catalog_hygiene:3637 delete_product_metafields namespace", lambda v: _build_hygiene([_s910_batch_resolve(_s910_triple_alias(metafield_id=None))])[0]["delete_product_metafields"](metafields=[{"ownerId": _PID, "namespace": v, "key": "k"}]), "ns4242", tail=True, tail_raw=True),
    _Row("catalog_hygiene:3637 delete_product_metafields key", lambda v: _build_hygiene([_s910_batch_resolve(_s910_triple_alias(metafield_id=None))])[0]["delete_product_metafields"](metafields=[{"ownerId": _PID, "namespace": "custom", "key": v}]), "key4242", tail=True, tail_raw=True),
    _Row("media/_reorder:64 reorder_product_media unknown ids", lambda v: _build_media([_product_media_read([_media_node(MEDIA_A)])])[0]["reorder_product_media"](product_id="123", moves=[{"id": v, "newPosition": 1}]), "gid://shopify/MediaImage/4242"),
    _Row("webhooks:176 delete_webhook", lambda v: _build_webhooks([])[0]["delete_webhook"](subscription_id=v), "4242", refused=f"Error: {INVALID_SUBSCRIPTION_ID_ERROR}."),
    # Review round 1, the post-lookup class: `typed_gid` strips before the read, so a
    # valid id followed by CR/LF or spaces is found, and the echo showed it raw.
    # One row per module and reply shape; the guard holds every other site.
    _Row("post-lookup inventory get_inventory", _inv("get_inventory", [_product_with_variants(_ONE_VARIANT)], arg="product_id"), _PID, **_trailing(_PID)),
    _Row("post-lookup inventory tracking preview", _inv("update_variant_inventory_tracking", [_product_with_variants(_ONE_VARIANT)], arg="product_id", tracked=False), _PID, **_trailing(_PID)),
    _Row("post-lookup products policy preview", lambda v: _build_products([_variants_policy_read([_variant_policy("10", "S", "CONTINUE")])])[0]["update_variant_inventory_policy"](product_id=v, new_policy="DENY"), _PID, **_trailing(_PID)),
    _Row("post-lookup media update_product_media", lambda v: _build_media([_product_media_read([_media_node(MEDIA_A)])])[0]["update_product_media"](product_id=v, media_id=MEDIA_A, alt="x"), _PID, **_trailing(_PID)),
    _Row("post-lookup media delete_product_media", lambda v: _build_media([_product_media_read([_media_node(MEDIA_A)])])[0]["delete_product_media"](product_id=v, media_ids=[MEDIA_A]), _PID, **_trailing(_PID)),
    _Row("post-lookup collections handle", lambda v: _build_collections([_manual_collection()])[0]["add_product_to_collection"](handle=v, product_id="4242"), "vanish", **_trailing("vanish")),
    _Row("post-lookup catalog_hygiene update_product_pricing", lambda v: _build_hygiene([_read_response()])[0]["update_product_pricing"](v, variants=[{"variantId": "gid://shopify/ProductVariant/201", "price": "49.99"}]), _PID, tail=True, **_trailing(_PID)),
]  # fmt: skip


def test_s10103_metafield_keys_line_is_bounded_as_a_whole() -> None:
    """The keys line is one value (`keys_display`), so the bound applies to the
    whole list, not to each key: many keys cannot add up past REFLECT_MAX_LEN."""
    keys = [f"k{i:03d}" for i in range(100)]
    tools, _fc = _build_hygiene([_s911_product_response([])])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, keys=keys)
    display = ", ".join(f'"{k}"' for k in keys)
    assert len(display) > REFLECT_MAX_LEN
    assert f"No metafields found for keys: {display[:REFLECT_MAX_LEN]}\n" in out
    assert display not in out


_S10103_IDS = [r.site for r in S10103_ROWS]


def _s10103_expected(row: _Row, value: str) -> tuple[str, Any]:
    """The ordinary reply with each echo of `row.short` swapped for the scrubbed value."""
    template = row.run(row.short)
    old, new = row.shown(row.short), _scrubbed(row.shown(value))
    if not row.tail:
        assert template.count(old) == row.echoes, template
        return template.replace(old, new), None
    text, tail = _with_tail(template)
    assert text.count(old) == row.echoes, text
    return text.replace(old, new), _swap(tail, old, row.shown(value) if row.tail_raw else new)


def _s10103_assert(row: _Row, value: str) -> None:
    out = row.run(value)
    head, tail = _s10103_expected(row, value)
    if not row.tail:
        assert out == head
        return
    out_head, out_tail = _with_tail(out)
    assert out_head == head
    assert out_tail == tail


@pytest.mark.parametrize("row", S10103_ROWS, ids=_S10103_IDS)
def test_s10103_caps_an_over_long_identifier(row: _Row) -> None:
    _s10103_assert(row, row.long)
    out = row.run(row.long)
    assert row.long not in (_with_tail(out)[0] if row.tail else out)


@pytest.mark.parametrize("row", S10103_ROWS, ids=_S10103_IDS)
def test_s10103_cannot_forge_a_second_line(row: _Row) -> None:
    out = row.run(row.crlf)
    if row.refused is not None:
        # An earlier GID rule refuses CR/LF before any echo (Story 10.110).
        assert out == row.refused
        return
    _s10103_assert(row, row.crlf)
    assert _CR not in out and row.crlf not in out


@pytest.mark.parametrize("row", S10103_ROWS, ids=_S10103_IDS)
def test_s10103_ordinary_identifier_is_byte_identical(row: _Row) -> None:
    """The ordinary reply echoes the value raw, as before this story (AC 4)."""
    out = row.run(row.short)
    text = out if not row.tail else _with_tail(out)[0]
    assert text.count(row.shown(row.short)) == row.echoes


def test_s10103_swap_helper_reaches_nested_tail_values() -> None:
    """`_swap` must rewrite strings at any depth, or a tail echo would go unchecked."""
    tail = json.loads('{"a": ["x1", {"b": "x1x"}], "n": 1}')
    assert _swap(tail, "x1", "Y") == {"a": ["Y", {"b": "Yx"}], "n": 1}
