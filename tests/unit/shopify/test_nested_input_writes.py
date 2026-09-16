"""Guard: a partial nested-input write cannot be added silently (Story 9.19).

**The bug this exists to stop recurring.** ``productUpdate`` REPLACES a nested
input object wholesale. Sending ``product: {id, seo: {title}}`` does not leave
``seo.description`` alone — it CLEARS it. That is the opposite of the top-level
behaviour, where omitting ``tags`` or ``status`` from ``ProductUpdateInput``
really does leave them untouched, and the asymmetry is exactly why
``update_product_seo`` shipped destroying SEO descriptions. It was found only by
a live round-trip with a full-object diff: ``{"seo": {"title": ...}}`` is a
valid ``SEOInput``, so document validation and variable coercion (the two legs
of ``test_admin_schema_contract.py``) both accept it. They check SHAPE; this is
SEMANTICS.

**Why a registry, and why it is discovered rather than listed.** A point-in-time
sweep is what let the bug reach production. So this module does not list the
mutation call sites it knows about — it FINDS every function in
``shopify_mcp.shopify`` and ``shopify_mcp.tools`` whose code names a mutation
document, and requires each to carry a recorded verdict in ``_VERDICTS``. A new
write, anywhere in those packages, fails here until someone has decided whether
it sends a nested object into an update. The equality runs both ways, so a
removed or renamed site fails too rather than leaving a stale verdict behind.

**Why ``productUpdate`` callers are also emitted.** A verdict is a human
reading. For the one mutation PROVEN to replace nested inputs wholesale, that is
not enough: every call site is run, its payload captured, and any nested input
object in it must carry every field the pinned schema declares for its type. So
an existing scalar-only caller that later starts sending a partial ``seo`` fails
too — not just a brand-new site. Other mutations' nested semantics are
UNVERIFIED live (see the per-site verdicts and ``docs/tech-debt.md``); none of
them sends a nested object into an update today.

Scope note: the walk covers ``shopify_mcp.shopify`` and ``shopify_mcp.tools``
(including ``tools/media``, which executes its own mutations) — every mutation
document in the package lives there. ``server``/``__main__`` are not imported:
importing them has start-up side effects and they define no documents.
"""

import importlib
import pkgutil
from collections.abc import Callable, Iterator
from types import CodeType
from typing import Any

import pytest
from graphql import (
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    OperationDefinitionNode,
    OperationType,
    parse,
)
from graphql.error import GraphQLError
from graphql.language import print_ast

import shopify_mcp.shopify
import shopify_mcp.tools
from shopify_mcp.shopify.operations import catalog_hygiene as hygiene_ops
from shopify_mcp.shopify.operations import products as ops
from tests.unit.shopify.test_admin_schema_contract import _SCHEMA

# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

CREATE = "create — sends a whole new object; nothing stored exists to clear"
SCALAR_UPDATE = (
    "update — top-level scalar / list-of-scalar fields only, no nested object; "
    "omitted top-level fields are left alone"
)
ENTRY_LIST = (
    "list of self-contained entries (identifiers, or complete records per entry) "
    "— no nested object inside an entry"
)
NESTED_COMPLETE = (
    "update carrying a nested input object — sent COMPLETE via read-modify-write, "
    "enforced by test_product_update_payloads_send_nested_inputs_complete"
)

_OPS = "shopify_mcp.shopify.operations"
_MEDIA = "shopify_mcp.tools.media"

_VERDICTS: dict[str, str] = {
    # ---- productUpdate — PROVEN to replace nested inputs wholesale (9.19) ----
    f"{_OPS}.products:update_product_seo": NESTED_COMPLETE,
    f"{_OPS}.products:update_product_title": SCALAR_UPDATE,
    f"{_OPS}.products:update_product_description": SCALAR_UPDATE,
    f"{_OPS}.products:update_product_tags": SCALAR_UPDATE,
    f"{_OPS}.products:update_product_status": SCALAR_UPDATE,
    f"{_OPS}.catalog_hygiene:update_product_category": SCALAR_UPDATE,
    f"{_OPS}.catalog_hygiene:update_product_vendor": SCALAR_UPDATE,
    f"{_OPS}.catalog_hygiene:update_product_type": SCALAR_UPDATE,
    # ---- other updates — nested semantics unverified live; none sends one ----
    # collectionUpdate: `{id, title?, descriptionHtml?}`. CollectionInput has a
    # nested `seo` too — it is NOT sent. Sending it would need the same fix.
    f"{_OPS}.collections:update_collection": SCALAR_UPDATE,
    # inventoryItemUpdate: `input: {tracked}`.
    f"{_OPS}.inventory:update_inventory_item_tracked": SCALAR_UPDATE,
    # productVariantsBulkUpdate: one `{id, <scalar>}` entry per variant.
    f"{_OPS}.products:update_variant_inventory_policy": ENTRY_LIST,
    f"{_OPS}.catalog_hygiene:update_variants_pricing": ENTRY_LIST,
    # productOptionUpdate: `option: {id, name}`, `optionValuesToUpdate: [{id, name}]`.
    f"{_OPS}.catalog_hygiene:update_product_option": SCALAR_UPDATE,
    # productUpdateMedia: `[{id, alt}]` — one entry per media, scalars only.
    f"{_MEDIA}._update:register.<locals>.update_product_media": ENTRY_LIST,
    # ---- creates / sets of complete records ----
    f"{_OPS}.collections:create_collection": CREATE,
    f"{_OPS}.discounts:create_discount_code_basic": CREATE,
    f"{_OPS}.webhooks:create_webhook": CREATE,
    f"{_OPS}.catalog_hygiene:set_metafields": ENTRY_LIST,
    f"{_OPS}.inventory:set_inventory_on_hand": ENTRY_LIST,
    f"{_MEDIA}._upload:_stage_upload": CREATE,
    f"{_MEDIA}._upload:_attach_media": CREATE,
    # ---- identifiers only: membership, publication, media moves/deletes ----
    # Membership sites name the document and hand it to `_change_membership`,
    # which executes it — discovery attributes the write to the naming site.
    f"{_OPS}.collections:add_products_to_collection": ENTRY_LIST,
    f"{_OPS}.collections:remove_products_from_collection": ENTRY_LIST,
    f"{_MEDIA}._upload:_maybe_reorder_new_media": ENTRY_LIST,
    f"{_OPS}.publications:publish": ENTRY_LIST,
    f"{_OPS}.publications:unpublish": ENTRY_LIST,
    f"{_OPS}.catalog_hygiene:append_variant_media": ENTRY_LIST,
    f"{_OPS}.catalog_hygiene:detach_variant_media": ENTRY_LIST,
    f"{_OPS}.catalog_hygiene:delete_metafields": ENTRY_LIST,
    f"{_OPS}.webhooks:delete_webhook": ENTRY_LIST,
    f"{_MEDIA}._reorder:register.<locals>.reorder_product_media": ENTRY_LIST,
    f"{_MEDIA}._delete:register.<locals>.delete_product_media": ENTRY_LIST,
}

# Mutations proven live to REPLACE a nested input wholesale. Every call site of
# one of these must be emitted below, not merely classified.
_REPLACES_NESTED_WHOLESALE = {"productUpdate"}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _modules() -> Iterator[Any]:
    for package in (shopify_mcp.shopify, shopify_mcp.tools):
        yield package
        for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
            yield importlib.import_module(info.name)


def _mutation(value: object) -> OperationDefinitionNode | None:
    if not isinstance(value, str) or "mutation" not in value:
        return None
    try:
        document = parse(value)
    except GraphQLError:
        return None
    for definition in document.definitions:
        if (
            isinstance(definition, OperationDefinitionNode)
            and definition.operation is OperationType.MUTATION
        ):
            return definition
    return None


def _mutation_documents() -> dict[str, OperationDefinitionNode]:
    """Module-level constant NAME -> parsed mutation, across both packages."""
    found: dict[str, OperationDefinitionNode] = {}
    for module in _modules():
        for name, value in vars(module).items():
            operation = _mutation(value)
            if operation is None:
                continue
            previous = found.get(name)
            assert previous is None or print_ast(previous) == print_ast(operation), (
                f"two different mutation documents share the constant name {name!r}; "
                "call-site discovery keys on the name — rename one"
            )
            found[name] = operation
    return found


def _code_objects(code: CodeType) -> Iterator[CodeType]:
    yield code
    for const in code.co_consts:
        if isinstance(const, CodeType):
            yield from _code_objects(const)


def _call_sites() -> dict[str, set[str]]:
    """`module:qualname` of every function naming a mutation document -> root fields."""
    documents = _mutation_documents()
    sites: dict[str, set[str]] = {}
    for module in _modules():
        for value in vars(module).values():
            if not callable(value) or getattr(value, "__module__", None) != module.__name__:
                continue
            code = getattr(value, "__code__", None)
            if code is None:
                continue
            for inner in _code_objects(code):
                named = documents.keys() & set(inner.co_names)
                if not named:
                    continue
                roots = sites.setdefault(f"{module.__name__}:{inner.co_qualname}", set())
                for name in named:
                    roots.update(
                        sel.name.value  # type: ignore[attr-defined]
                        for sel in documents[name].selection_set.selections
                    )
    return sites


def test_every_mutation_call_site_has_a_recorded_verdict():
    """Fails on a NEW write site, not only on today's — the point of this card."""
    discovered = set(_call_sites())
    unclassified = sorted(discovered - _VERDICTS.keys())
    stale = sorted(_VERDICTS.keys() - discovered)
    assert not unclassified, (
        "new mutation call site(s) with no nested-input verdict. productUpdate "
        "REPLACES nested input objects wholesale — an omitted key is CLEARED. "
        "Decide whether this site sends a nested object into an update and record "
        f"it in _VERDICTS: {unclassified}"
    )
    assert not stale, f"_VERDICTS names sites that no longer exist: {stale}"


def test_discovery_sees_the_site_that_shipped_the_bug():
    """Spelled independently of `_VERDICTS`, so an empty or broken discovery
    cannot pass the equality above by agreeing with an equally broken table."""
    sites = _call_sites()
    assert sites[f"{_OPS}.products:update_product_seo"] == {"productUpdate"}
    assert sites[f"{_MEDIA}._update:register.<locals>.update_product_media"] == {
        "productUpdateMedia"
    }
    assert len(sites) >= 32


# ---------------------------------------------------------------------------
# Emitted-payload completeness for productUpdate
# ---------------------------------------------------------------------------


class _CapturingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((query, variables or {}))
        return {}


_PID = "6803111739545"
_GID = f"gid://shopify/Product/{_PID}"

_EMITTERS: dict[str, Callable[[_CapturingClient], object]] = {
    f"{_OPS}.products:update_product_seo": lambda c: ops.update_product_seo(
        c, _PID, title="S", description=None
    ),
    f"{_OPS}.products:update_product_title": lambda c: ops.update_product_title(c, _PID, "T", "t"),
    f"{_OPS}.products:update_product_description": lambda c: ops.update_product_description(
        c, _PID, "<p>d</p>"
    ),
    f"{_OPS}.products:update_product_tags": lambda c: ops.update_product_tags(c, _PID, ["a"]),
    f"{_OPS}.products:update_product_status": lambda c: ops.update_product_status(c, _PID, "DRAFT"),
    f"{_OPS}.catalog_hygiene:update_product_category": lambda c: (
        hygiene_ops.update_product_category(c, _GID, "gid://shopify/TaxonomyCategory/aa-1")
    ),
    f"{_OPS}.catalog_hygiene:update_product_vendor": lambda c: hygiene_ops.update_product_vendor(
        c, _GID, "V"
    ),
    f"{_OPS}.catalog_hygiene:update_product_type": lambda c: hygiene_ops.update_product_type(
        c, _GID, "Tee"
    ),
}


def _named_input(type_: Any) -> GraphQLInputObjectType | None:
    while isinstance(type_, GraphQLNonNull | GraphQLList):
        type_ = type_.of_type
    return type_ if isinstance(type_, GraphQLInputObjectType) else None


def _incomplete_nested_inputs(query: str, variables: dict[str, Any]) -> list[str]:
    """Every nested input object in `variables` that omits a declared field.

    A variable's own input object (or each entry of a list of them) is a root:
    its omitted TOP-level fields are left alone by Shopify. Any input object
    nested INSIDE a root is replaced wholesale, so it must be complete."""
    operation = parse(query).definitions[0]
    assert isinstance(operation, OperationDefinitionNode)
    problems: list[str] = []

    def walk(value: Any, type_: Any, path: str, is_root: bool) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, type_, f"{path}[]", is_root)
            return
        input_type = _named_input(type_)
        if input_type is None or not isinstance(value, dict):
            return
        if not is_root:
            missing = sorted(input_type.fields.keys() - value.keys())
            if missing:
                problems.append(f"{path} ({input_type.name}) omits {missing}")
        for key, child in value.items():
            assert key in input_type.fields, f"{path}.{key} is not a field of {input_type.name}"
            walk(child, input_type.fields[key].type, f"{path}.{key}", is_root=False)

    for definition in operation.variable_definitions:
        name = definition.variable.name.value
        schema_type = _SCHEMA.get_type(print_ast(definition.type).strip("[]!"))
        walk(variables.get(name), schema_type, f"${name}", is_root=True)
    return problems


def test_every_wholesale_replacing_call_site_is_emitted():
    """A new productUpdate caller must join `_EMITTERS`, or the completeness
    check below would not see it."""
    replacing = {
        site for site, roots in _call_sites().items() if roots & _REPLACES_NESTED_WHOLESALE
    }
    assert replacing == _EMITTERS.keys()


@pytest.mark.parametrize("site", sorted(_EMITTERS))
def test_product_update_payloads_send_nested_inputs_complete(site):
    client = _CapturingClient()
    _EMITTERS[site](client)
    assert len(client.calls) == 1
    query, variables = client.calls[0]
    assert _incomplete_nested_inputs(query, variables) == []


def test_completeness_check_rejects_the_payload_that_destroyed_live_data():
    """The discriminating negative: the exact shape `update_product_seo` sent on
    2026-09-15 — schema-valid, coercible, and data-destroying."""
    client = _CapturingClient()
    ops.update_product_seo(client, _PID, title="S", description=None)
    query, _ = client.calls[0]
    broken = {"product": {"id": _GID, "seo": {"title": "S"}}}
    assert _incomplete_nested_inputs(query, broken) == [
        "$product.seo (SEOInput) omits ['description']"
    ]


def test_completeness_check_leaves_omitted_top_level_fields_alone():
    """The asymmetry itself: omitting a ROOT field (tags, status…) is safe."""
    client = _CapturingClient()
    ops.update_product_title(client, _PID, "T", "t")
    query, variables = client.calls[0]
    assert "seo" not in variables["product"]
    assert _incomplete_nested_inputs(query, variables) == []
