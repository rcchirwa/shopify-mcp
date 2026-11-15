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
- Story 10.103 rows (`S10103_ROWS`): the same echo in other wordings. Story
  10.113 adds the caller-id joins (F1-F4: each id sanitized, the joined list
  bounded) and the channel-name field of `_render_failed`'s rows (F5).

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
    _S96_MEDIA_CROSS,
    _S96_VARIANT_A,
    _S911_PRODUCT_GID,
    _options_read_response,
    _parse_tail,
    _read_response,
    _s96_combined_response,
    _s96_mutation_response,
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
from tests.unit.tools.test_publications import _build as _build_publications
from tests.unit.tools.test_publications import (
    _channels_response,
    _collection_pubs,
    _product_pubs,
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
    # Story 9.29: the pricing tool now echoes `_identifier_channel`'s `product_ref`.
    "catalog_hygiene:1689 update_product_pricing": 200,
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
        rep: bool = False,
        tail_shown: Callable[[str], str] | None = None,
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
        # A `!r` site shows repr(value), which doubles the escape backslashes.
        self.rep = rep
        # How the JSON tail renders the value, when its bound differs from the
        # head's (Story 10.113: the head's joined list is `_cap`ped at 200, the
        # tail's per-variant messages at REFLECT_MAX_LEN).
        self.tail_shown = tail_shown

    def old(self, value: str) -> str:
        return repr(self.shown(value)) if self.rep else self.shown(value)

    def new(self, value: str) -> str:
        scrubbed = _scrubbed(self.shown(value))
        return repr(scrubbed) if self.rep else scrubbed


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


def _drained(
    build: Callable, script: Callable[[], list], call: Callable[[dict, str], str]
) -> Callable[[str], str]:
    """Run one tool call on a fresh script. Every scripted response must be
    read, so the row proves the path it names ran (Story 10.113)."""

    def run(value: str) -> str:
        tools, fc = build(script())
        out = call(tools, value)
        assert fc.responses == [], fc.responses
        return out

    return run


def _gid_tail(value: str) -> str:
    """`from_gid`, spelled independently: the text after the last `/`."""
    return value.rsplit("/", 1)[-1]


_V97531 = "gid://shopify/ProductVariant/97531"
_CHANNEL = "Typo Channel"


def _binding_read() -> list:
    return [_s96_combined_response([_S96_MEDIA_1], [(_S96_VARIANT_A, "sku-a", [])])]


def _channel_reads(*, write: bool) -> list:
    """Both channel reads (the second is the refresh on a missed name), then
    the product read; `write` adds the V2 page the unpublish and set tools read."""
    return [
        _channels_response(),
        _channels_response(),
        _product_pubs(pid="123", published_ids=[1], not_published_ids=[2, 3, 4], write=write),
    ]


def _collection_reads() -> list:
    return [_channels_response(), _channels_response(), _collection_pubs(published_ids=[1])]


def _pub(tool: str, *, write: bool, **kw: Any) -> Callable[[str], str]:
    return _drained(
        _build_publications,
        lambda: _channel_reads(write=write),
        lambda t, v: t[tool](product_id="123", channel_names=[v], **kw),
    )


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
    _Row("post-lookup catalog_hygiene update_product_pricing", lambda v: _build_hygiene([_read_response()])[0]["update_product_pricing"](v, variants=[{"variantId": "gid://shopify/ProductVariant/201", "price": "49.99"}]), _PID, tail=True, shown=lambda v: v[:200], **_trailing(_PID)),
    # Review round 2: `product_ref` (`_identifier_channel` caps it at 200, but did not sanitize it).
    _Row("catalog_hygiene:2976 image binding preview product_ref", lambda v: _build_hygiene([_s96_combined_response([_S96_MEDIA_1], [(_S96_VARIANT_A, "sku-a", [])])])[0]["update_variant_image_binding"](product_id=v, **_BINDING), _PID, tail=True, shown=lambda v: v[:200]),
    _Row("catalog_hygiene:2858 image binding variant not on product", lambda v: _build_hygiene([_s96_combined_response([_S96_MEDIA_1], [(_S96_VARIANT_A, "sku-a", [])])])[0]["update_variant_image_binding"](product_id=v, variant_media=[{"variantId": "gid://shopify/ProductVariant/999", "mediaIds": [_S96_MEDIA_1]}]), _PID, tail=True, shown=lambda v: v[:200]),
    _Row("catalog_hygiene:2870 image binding media not on product", lambda v: _build_hygiene([_s96_combined_response([_S96_MEDIA_1], [(_S96_VARIANT_A, "sku-a", [])])])[0]["update_variant_image_binding"](product_id=v, variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_CROSS]}]), _PID, tail=True, shown=lambda v: v[:200]),
    _Row("catalog_hygiene:3019 image binding no-op", lambda v: _build_hygiene([_s96_combined_response([_S96_MEDIA_1], [(_S96_VARIANT_A, "sku-a", [_S96_MEDIA_1])])])[0]["update_variant_image_binding"](product_id=v, confirm=True, **_BINDING), _PID, tail=True, shown=lambda v: v[:200]),
    _Row("catalog_hygiene:3157 image binding confirmed", lambda v: _build_hygiene([_s96_combined_response([_S96_MEDIA_1], [(_S96_VARIANT_A, "sku-a", [])]), _s96_mutation_response(productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "", None)])])])[0]["update_variant_image_binding"](product_id=v, confirm=True, **_BINDING), _PID, tail=True, shown=lambda v: v[:200]),
    _Row("catalog_hygiene:2371 update_product_vendor not found", lambda v: _build_hygiene([{"productByHandle": None}])[0]["update_product_vendor"](handle=v, vendor="V"), "vanish-tee", tail=True, shown=lambda v: v[:200], rep=True),
    _Row("catalog_hygiene:2560 update_product_type not found", lambda v: _build_hygiene([{"productByHandle": None}])[0]["update_product_type"](handle=v, product_type="T"), "vanish-tee", tail=True, shown=lambda v: v[:200], rep=True),
    _Row("catalog_hygiene:4181 update_product_options not found", lambda v: _build_hygiene([{"productByHandle": None}])[0]["update_product_options"](handle=v, option={"id": _OPT_GID, "name": "Size"}), "vanish-tee", tail=True, shown=lambda v: v[:200], rep=True),
    # Review round 3: ids that a normalize helper returns and the reply joins
    # into one list. The helpers only strip the ends and check the GID prefix.
    _Row("catalog_hygiene:2870 image binding media ids joined", lambda v: _build_hygiene([_s96_combined_response([_S96_MEDIA_1], [(_S96_VARIANT_A, "sku-a", [])])])[0]["update_variant_image_binding"](product_id=_PID, variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [v]}]), "gid://shopify/MediaImage/94242", tail=True, shown=lambda v: v[:200]),
    _Row("catalog_hygiene:4241 update_product_options value ids joined", lambda v: _build_hygiene([_options_read_response()])[0]["update_product_options"](product_id="100", option={"id": _OPT_GID}, option_values_to_update=[{"id": v, "name": "X"}]), "gid://shopify/ProductOptionValue/94242", tail=True, shown=lambda v: v[:200]),
    # Story 10.113 (SEC-24-joined-ids): caller ids joined into one list with `+`
    # (F1-F4), and the caller channel-name field of `_render_failed`'s rows (F5).
    # Site lines are on origin/main 99e2315. The many-short-ids bound is
    # test_s10113_many_short_ids_are_bounded_as_a_whole, below.
    _Row("F1 catalog_hygiene:1952 update_product_pricing confirm ids joined", _drained(_build_hygiene, lambda: [_read_response()], lambda t, v: t["update_product_pricing"]("100", variants=[{"variantId": v, "price": "49.99"}], confirm=True)), _V97531, tail=True, shown=lambda v: _gid_tail(v)[:200], tail_shown=_gid_tail),
    _Row("F2 catalog_hygiene:2860 image binding variant ids joined", _drained(_build_hygiene, _binding_read, lambda t, v: t["update_variant_image_binding"](product_id=_PID, variant_media=[{"variantId": v, "mediaIds": [_S96_MEDIA_1]}])), _V97531, tail=True, shown=lambda v: _gid_tail(v)[:200]),
    _Row("F4 publications:466 set_product_publications refusal", _pub("set_product_publications", write=True, confirm=True), _CHANNEL),
    _Row("F5 publications:458 set_product_publications preview", _pub("set_product_publications", write=True, confirm=False), _CHANNEL),
    _Row("F5 publications:458 publish_product_to_channels preview", _pub("publish_product_to_channels", write=False, confirm=False), _CHANNEL),
    _Row("F5 publications:458 publish_product_to_channels FAILED", _pub("publish_product_to_channels", write=False, confirm=True), _CHANNEL),
    _Row("F5 publications:458 unpublish_product_from_channels FAILED", _pub("unpublish_product_from_channels", write=True, confirm=True), _CHANNEL),
    _Row("F5 publications:458 publish_collection_to_channels FAILED", _drained(_build_publications, _collection_reads, lambda t, v: t["publish_collection_to_channels"](handle="all-copy", channel_names=[v], confirm=True)), _CHANNEL),
]  # fmt: skip


@pytest.mark.parametrize("move_id", ["4242" + "7" * 400, "4242" + _INJECT], ids=["long", "crlf"])
def test_s10103_malformed_move_echo_is_bounded(move_id: str) -> None:
    """reorder_product_media echoes a malformed move whole (`repr`). `repr`
    already escapes CR/LF; review round 2 bounds its length (media/_reorder.py)."""
    move = {"id": move_id, "newPosition": 0}
    tools, _fc = _build_media([])
    out = tools["reorder_product_media"](product_id="123", moves=[move])
    expected = "Error: each move needs id (string) and newPosition (int >= 1). Got: "
    assert out == expected + _scrubbed(repr(move))
    assert _CR not in out and len(out) <= len(expected) + REFLECT_MAX_LEN


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
    old, new = row.old(row.short), row.new(value)
    if not row.tail:
        assert template.count(old) == row.echoes, template
        return template.replace(old, new), None
    text, tail = _with_tail(template)
    assert text.count(old) == row.echoes, text
    if row.tail_shown is not None:
        return text.replace(old, new), _swap(tail, old, _scrubbed(row.tail_shown(value)))
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
    assert text.count(row.old(row.short)) == row.echoes


def test_s10103_swap_helper_reaches_nested_tail_values() -> None:
    """`_swap` must rewrite strings at any depth, or a tail echo would go unchecked."""
    tail = json.loads('{"a": ["x1", {"b": "x1x"}], "n": 1}')
    assert _swap(tail, "x1", "Y") == {"a": ["Y", {"b": "Yx"}], "n": 1}


# ---------- Story 10.113: the joined list is bounded as a whole ----------
#
# Each item is short (30 characters), so a per-item bound changes nothing; the
# 25 items (the per-call cap of each tool) joined are far past REFLECT_MAX_LEN.
# Only a bound on the joined text keeps the reply short.

_MANY = 25
_HYGIENE_BOUND = 200  # catalog_hygiene's `_cap` (`_GID_DISPLAY_MAX`)
_REFUSAL_TAIL = (
    ". Nothing was changed — a declarative set with an unknown name would "
    "unpublish every channel not named. Correct the names and retry."
)


def _thirty_digit_tails() -> list[str]:
    return [str(10**29 + i) for i in range(_MANY)]


def _many_pricing() -> tuple[str, str, Any]:
    tails = _thirty_digit_tails()
    variants = [{"variantId": f"gid://shopify/ProductVariant/{t}", "price": "1.00"} for t in tails]
    run = _drained(
        _build_hygiene,
        lambda: [_read_response()],
        lambda t, _v: t["update_product_pricing"]("100", variants=variants, confirm=True),
    )
    joined = ", ".join(tails)
    head = "Error: resolved variant(s) not found on product: " + joined[:_HYGIENE_BOUND]
    errors = [{"message": f"variant {t} not found on product 100"} for t in tails]
    return run(""), joined, (head, {"ok": False, "variants": [], "errors": errors})


def _many_binding() -> tuple[str, str, Any]:
    tails = _thirty_digit_tails()
    entries = [
        {"variantId": f"gid://shopify/ProductVariant/{t}", "mediaIds": [_S96_MEDIA_1]}
        for t in tails
    ]
    run = _drained(
        _build_hygiene,
        _binding_read,
        lambda t, _v: t["update_variant_image_binding"](product_id=_PID, variant_media=entries),
    )
    joined = ", ".join(tails)
    msg = f"variant GIDs not on product {_PID}: " + joined[:_HYGIENE_BOUND]
    return (
        run(""),
        joined,
        ("Error: " + msg, {"ok": False, "variants": [], "errors": [{"message": msg}]}),
    )


def _many_reorder() -> tuple[str, str, Any]:
    ids = [f"gid://shopify/MediaImage/{10000 + i}" for i in range(_MANY)]
    moves = [{"id": m, "newPosition": i + 1} for i, m in enumerate(ids)]
    run = _drained(
        _build_media,
        lambda: [_product_media_read([_media_node(MEDIA_A)])],
        lambda t, _v: t["reorder_product_media"](product_id="123", moves=moves),
    )
    joined = ", ".join(ids)
    head = "Error: these media ids are not attached to the product: " + joined[:REFLECT_MAX_LEN]
    return run(""), joined, (head, None)


def _many_channels() -> tuple[str, str, Any]:
    names = [f"Typo Channel {i:017d}" for i in range(_MANY)]
    run = _drained(
        _build_publications,
        lambda: _channel_reads(write=True),
        lambda t, _v: t["set_product_publications"](
            product_id="123", channel_names=names, confirm=True
        ),
    )
    joined = ", ".join(names)
    head = "Error: could not resolve channel name(s): " + joined[:REFLECT_MAX_LEN] + _REFUSAL_TAIL
    return run(""), joined, (head, None)


@pytest.mark.parametrize(
    "case",
    [_many_pricing, _many_binding, _many_reorder, _many_channels],
    ids=["F1 pricing confirm", "F2 image binding", "F3 reorder", "F4 set refusal"],
)
def test_s10113_many_short_ids_are_bounded_as_a_whole(case: Callable) -> None:
    out, joined, (head, tail) = case()
    item_lengths = {len(item) for item in joined.split(", ")}
    assert item_lengths == {30}, item_lengths
    assert len(joined) > REFLECT_MAX_LEN
    if tail is None:
        assert out == head
    else:
        assert _with_tail(out) == (head + "\n\n```json```", tail)
    assert joined not in out


def test_s10113_two_unresolved_names_stay_on_their_own_rows() -> None:
    """F5: each failed channel is one row; a CR/LF in a name escapes inside its
    row, and the `\\n` between rows is kept."""
    names = ["Typo A" + _INJECT, "Typo B" + _INJECT]
    tools, fc = _build_publications(_channel_reads(write=True))
    out = tools["set_product_publications"](product_id="123", channel_names=names)
    assert fc.responses == []
    assert out == (
        "PREVIEW — Set product publications (declarative)\n"
        "  Product: Tee (handle: tee, id: 123)\n"
        "  Would add (publish):\n"
        "  (none)\n"
        "  Would remove (unpublish):\n"
        "  • Online Store (id: 1)\n"
        "  Unchanged:\n"
        "  (none)\n"
        "  Failed to resolve:\n"
        "  • Typo A\\r\\nInjected: yes: channel not found on this store\n"
        "  • Typo B\\r\\nInjected: yes: channel not found on this store\n"
        "\n"
        "To apply, call again with confirm=True."
    )
    assert _CR not in out
    assert not any(line.startswith("Injected") for line in out.split(_LF))


# Ordinary values, pinned as literal text captured on origin/main 99e2315
# before the fix: the scrub must not change a byte of them (AC 3).
_ORDINARY_BEFORE = {
    "F1": (
        _S10103_IDS.index("F1 catalog_hygiene:1952 update_product_pricing confirm ids joined"),
        "Error: resolved variant(s) not found on product: 97531\n\n```json\n"
        '{\n  "ok": false,\n  "variants": [],\n  "errors": [\n    {\n'
        '      "message": "variant 97531 not found on product 100"\n    }\n  ]\n}\n```',
    ),
    "F2": (
        _S10103_IDS.index("F2 catalog_hygiene:2860 image binding variant ids joined"),
        "Error: variant GIDs not on product gid://shopify/Product/4242: 97531\n\n```json\n"
        '{\n  "ok": false,\n  "variants": [],\n  "errors": [\n    {\n'
        '      "message": "variant GIDs not on product gid://shopify/Product/4242: 97531"\n'
        "    }\n  ]\n}\n```",
    ),
    "F4": (
        _S10103_IDS.index("F4 publications:466 set_product_publications refusal"),
        "Error: could not resolve channel name(s): Typo Channel" + _REFUSAL_TAIL,
    ),
    "F5 set preview": (
        _S10103_IDS.index("F5 publications:458 set_product_publications preview"),
        "PREVIEW — Set product publications (declarative)\n  Product: Tee (handle: tee, id: 123)\n"
        "  Would add (publish):\n  (none)\n  Would remove (unpublish):\n  • Online Store (id: 1)\n"
        "  Unchanged:\n  (none)\n  Failed to resolve:\n"
        "  • Typo Channel: channel not found on this store\n\nTo apply, call again with confirm=True.",
    ),
    "F5 publish preview": (
        _S10103_IDS.index("F5 publications:458 publish_product_to_channels preview"),
        "PREVIEW — Publish product to channels\n  Product: Tee (handle: tee, id: 123)\n"
        "  Would publish to:\n  (none)\n  Already published (unchanged):\n  (none)\n"
        "  Failed to resolve:\n  • Typo Channel: channel not found on this store\n\n"
        "To apply, call again with confirm=True.",
    ),
    "F5 publish FAILED": (
        _S10103_IDS.index("F5 publications:458 publish_product_to_channels FAILED"),
        "FAILED — Publish product to channels\n  Product: Tee (handle: tee, id: 123)\n"
        "  Now published to:\n  (none)\n  Unchanged:\n  (none)\n  Failed:\n"
        "  • Typo Channel: channel not found on this store",
    ),
    "F5 unpublish FAILED": (
        _S10103_IDS.index("F5 publications:458 unpublish_product_from_channels FAILED"),
        "FAILED — Unpublish product from channels\n  Product: Tee (handle: tee, id: 123)\n"
        "  Now unpublished from:\n  (none)\n  Unchanged:\n  (none)\n  Failed:\n"
        "  • Typo Channel: channel not found on this store",
    ),
    "F5 collection FAILED": (
        _S10103_IDS.index("F5 publications:458 publish_collection_to_channels FAILED"),
        "FAILED — Publish collection to channels\n"
        "  Collection: All (BACKUP) (handle: all-copy, id: 900)\n"
        "  Now published to:\n  (none)\n  Unchanged:\n  (none)\n  Failed:\n"
        "  • Typo Channel: channel not found on this store",
    ),
}


@pytest.mark.parametrize("key", list(_ORDINARY_BEFORE), ids=list(_ORDINARY_BEFORE))
def test_s10113_ordinary_value_reply_is_byte_identical_to_before(key: str) -> None:
    index, before = _ORDINARY_BEFORE[key]
    row = S10103_ROWS[index]
    assert row.run(row.short) == before


def test_s10113_ordinary_reorder_reply_is_byte_identical_to_before() -> None:
    """F3: the existing unknown-ids row (`media/_reorder:64`), pinned as text."""
    row = S10103_ROWS[_S10103_IDS.index("media/_reorder:64 reorder_product_media unknown ids")]
    assert row.run(row.short) == (
        "Error: these media ids are not attached to the product: gid://shopify/MediaImage/4242"
    )
