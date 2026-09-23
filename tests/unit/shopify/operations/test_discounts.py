"""
Offline unit tests for shopify.operations.discounts.

These exercise the operations layer DIRECTLY with a FakeClient — no MCP server,
no FastMCP import — proving the discounts operations are callable from non-MCP
entry points (Story 10.27 / A5, AC4). discounts has no by-id/by-handle read pair
and no duplicated selection set, so no shared GraphQL fragment is extracted
(AC3 — the "no forced fragment" decision is pinned by test_no_shared_fragment).

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/shopify/operations/test_discounts.py -v
"""

from graphql import (
    FieldNode,
    GraphQLSchema,
    InlineFragmentNode,
    OperationDefinitionNode,
    TypeInfo,
    TypeInfoVisitor,
    Visitor,
    build_schema,
    get_named_type,
    parse,
    visit,
)

from shopify_mcp.shopify.operations import discounts as ops
from shopify_mcp.shopify.queries import discounts as q
from tests.support import FakeClient
from tests.support.admin_schema_snapshot import SNAPSHOT_PATH

# ---------- AC3: no shared fragment applies to discounts ----------


def test_no_shared_fragment_in_discount_queries():
    """discounts has no by-id/by-handle pair and no duplicated selection set, so
    none of its GraphQL strings declares or spreads a fragment. GET_CODE_DISCOUNTS
    uses inline fragments (`... on DiscountCodeBasic`) to select type-specific
    fields off the `Discount` union — those are exempted, unlike a spread
    (`...FragmentName`) of a *named* fragment declaration."""
    assert "fragment " not in q.CREATE_DISCOUNT_CODE_BASIC
    assert "..." not in q.CREATE_DISCOUNT_CODE_BASIC
    assert "fragment " not in q.GET_CODE_DISCOUNTS


# ---------- Story 9.17: eligibility selection, PII guard ----------
#
# Round-1 shipped a field-name denylist here (walk the parsed document,
# assert "email" is not among the selected field names) and a round-2 review
# found it insufficient: a selection like
# `customers { id defaultEmailAddress { emailAddress } }` or
# `customers { __typename }` both select no field named "email" and so
# survive it, even though the first leaks an email address and the second
# leaks nothing useful but is still not what this query is supposed to send.
# The denylist-of-one-field-name test was REMOVED, not amended in place, once
# `test_get_code_discounts_customers_selection_is_exactly_id` below appeared
# to subsume it for this query's `customers { }` selection specifically.
#
# **Correction, round-3 review: that removal lost real, document-wide
# protection.** The claim (previously here) that "there is no other place in
# GET_CODE_DISCOUNTS an 'email' field could plausibly appear" was checked
# against the query as it stood, not against the schema's full reach from
# `DiscountNode`. The 2026-01 schema has PII paths that never pass through
# `context.customers` at all: `events { ... on CommentEvent { author { email
# phone firstName lastName } } }` (`author` resolves to `StaffMember`) and
# `metafield(s) { owner { ... on Customer { defaultEmailAddress {
# emailAddress } addresses note } } }` -- both `DiscountCodeNode.events` and
# `DiscountCodeNode.metafield` exist on 2026-01 and are exactly as reachable
# as the `customers` selection the narrower guard covers.
# `test_get_code_discounts_never_reaches_pii_beyond_customer_id` below
# restores document-wide protection: it walks the WHOLE document against the
# schema with graphql-core's `TypeInfo`, not one selection set, checking
# every field by the TYPE it resolves to rather than by name alone.
# `test_get_code_discounts_customers_selection_is_exactly_id` is kept as-is
# alongside it -- redundant for the one selection it covers, but a more
# targeted, more readable failure message for that specific case.


_PII_CARRYING_TYPES = frozenset(
    {"StaffMember", "MailingAddress", "CustomerEmailAddress", "CustomerPhoneNumber"}
)
_PII_FIELD_NAMES = frozenset({"email", "phone", "defaultEmailAddress"})


def _pii_violations(document_text: str, schema: GraphQLSchema) -> list[str]:
    """Every PII-risk violation `document_text` reaches against `schema`,
    walked with graphql-core's `TypeInfo` so each field is checked against
    the type it actually resolves to at that point in the tree (an inline
    fragment's type condition included), rather than by name alone:

    (a) a field whose PARENT type, or whose own FIELD type, is one of the
        PII-carrying types confirmed live on 2026-01 -- `StaffMember` (via
        `events { ... on CommentEvent { author { ... } } }`),
        `MailingAddress`/`CustomerEmailAddress`/`CustomerPhoneNumber` (via a
        `Customer`'s own fields, reachable from `metafield(s) { owner {
        ... on Customer { ... } } }`);
    (b) any field selected on `Customer` other than bare `id`;
    (c) a denylisted field name (`email`/`phone`/`defaultEmailAddress`)
        anywhere in the document, as a cheap belt-and-braces backstop
        alongside the typed checks above -- catches a rename of the parent
        type this guard does not yet know about.
    """
    document = parse(document_text)
    type_info = TypeInfo(schema)
    violations: list[str] = []

    class _Collector(Visitor):
        def enter_field(self, node: FieldNode, *_args: object) -> None:
            name = node.name.value
            if name.startswith("__"):
                return
            parent = type_info.get_parent_type()
            field_def = type_info.get_field_def()
            if parent is None or field_def is None:
                return  # not in the schema -- the contract suite reports this
            parent_name = get_named_type(parent).name
            field_type_name = get_named_type(field_def.type).name
            if parent_name in _PII_CARRYING_TYPES or field_type_name in _PII_CARRYING_TYPES:
                violations.append(
                    f"{parent_name}.{name} reaches PII-carrying type {field_type_name}"
                )
            if parent_name == "Customer" and name != "id":
                violations.append(f"Customer.{name} selected -- only `id` may be")
            if name in _PII_FIELD_NAMES:
                violations.append(f"field named {name!r} selected under {parent_name}")

    visit(document, TypeInfoVisitor(type_info, _Collector()))
    return violations


_PII_GUARD_SCHEMA = build_schema(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_get_code_discounts_never_reaches_pii_beyond_customer_id():
    """Document-wide PII guard (Story 9.17 round-3 review). Built against the
    COMMITTED schema snapshot -- the same file the contract suite validates
    documents against -- so a future selection that reaches a PII-carrying
    type is caught by the TYPE it resolves to, not by matching a field name
    a mutation might rename or nest differently.

    Proven to discriminate during review by three offline mutations, each
    with the schema snapshot refreshed from the mutated query FIRST (same
    offline regeneration script used to land this story), so the schema
    formally allows the mutated shape and the schema-CONTRACT test cannot be
    what fails it -- only this guard can: adding
    `events(first: 1) { nodes { ... on CommentEvent { author { email } } }
    }` under a `DiscountCodeNode`; adding `metafield(namespace: "x", key:
    "y") { owner { ... on Customer { defaultEmailAddress { emailAddress } }
    } }`; and `author { firstName lastName phone }` in place of `author {
    email }`. All three failed only this test."""
    assert _pii_violations(q.GET_CODE_DISCOUNTS, _PII_GUARD_SCHEMA) == []


def _operation(text: str) -> OperationDefinitionNode:
    return next(d for d in parse(text).definitions if isinstance(d, OperationDefinitionNode))


def _direct_field(selection_set, name: str) -> FieldNode | None:
    """The first direct-child field of `selection_set` named `name` — does not
    recurse, so it cannot be satisfied by a field nested somewhere else in the
    tree (the exact gap a bare substring/count check has)."""
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode) and selection.name.value == name:
            return selection
    return None


def _direct_inline_fragment(selection_set, type_name: str) -> InlineFragmentNode | None:
    for selection in selection_set.selections:
        if (
            isinstance(selection, InlineFragmentNode)
            and selection.type_condition is not None
            and selection.type_condition.name.value == type_name
        ):
            return selection
    return None


def _all_fields_named(document_text: str, name: str) -> list[FieldNode]:
    """Every FieldNode named `name` anywhere in the document, found by walking
    the parsed AST — see feedback_pin_graphql_by_parsing: a query assertion
    must survive the text changing while the suite stays green, which a
    substring/identity comparison cannot."""
    document = parse(document_text)
    found: list[FieldNode] = []

    def walk(selection_set):
        if selection_set is None:
            return
        for selection in selection_set.selections:
            if isinstance(selection, FieldNode) and selection.name.value == name:
                found.append(selection)
            walk(getattr(selection, "selection_set", None))

    for definition in document.definitions:
        walk(getattr(definition, "selection_set", None))
    return found


def test_get_code_discounts_customers_selection_is_exactly_id():
    """PII guard, structural (Story 9.17 review). The prior version of this
    guard (see the comment above) parsed the document correctly and checked
    field NAMES correctly — its weakness was being a denylist of exactly one
    name, `email`, which a selection like
    `customers { id defaultEmailAddress { emailAddress } }` or
    `customers { __typename }` both survive without ever selecting a field
    named `email`. This test asserts the SHAPE instead of a name: every
    `customers` selection set, on every `Discount` union member, must select
    exactly one field, `id`, with nothing nested under it — which bans
    `email` (and anything else) categorically rather than by name."""
    customers_fields = _all_fields_named(q.GET_CODE_DISCOUNTS, "customers")
    assert len(customers_fields) == 4, (
        f"expected a customers{{...}} selection on all 4 Discount union members, found "
        f"{len(customers_fields)}"
    )
    for field in customers_fields:
        selection_set = field.selection_set
        assert selection_set is not None and len(selection_set.selections) == 1
        (only,) = selection_set.selections
        assert isinstance(only, FieldNode)
        assert only.name.value == "id"
        assert only.selection_set is None


_DISCOUNT_CODE_TYPES = (
    "DiscountCodeBasic",
    "DiscountCodeBxgy",
    "DiscountCodeFreeShipping",
    "DiscountCodeApp",
)


def test_get_code_discounts_selects_full_eligibility_shape_on_every_member():
    """Approach 2 (Story 9.17, see docs/tech-debt.md): eligibility is a
    property of WHO may redeem the code, not of the reward type, so it is
    selected identically on all four `Discount` union members rather than
    only `DiscountCodeBasic`.

    Parsed and walked structurally (Story 9.17 review) rather than counted as
    a substring: a `q.GET_CODE_DISCOUNTS.count(...)` check stays at the
    expected number when one member's block is commented out and another
    member's block is duplicated in its place, and does not notice a field
    dropped from inside one specific member (e.g. `segments { id }` losing
    `name` only under `DiscountCodeBasic`) while the totals still match."""
    operation = _operation(q.GET_CODE_DISCOUNTS)
    discount_field = _direct_field(operation.selection_set, "discountNodes")
    assert discount_field is not None
    nodes_field = _direct_field(discount_field.selection_set, "nodes")
    assert nodes_field is not None
    discount_inner = _direct_field(nodes_field.selection_set, "discount")
    assert discount_inner is not None

    for type_name in _DISCOUNT_CODE_TYPES:
        fragment = _direct_inline_fragment(discount_inner.selection_set, type_name)
        assert fragment is not None, f"missing `... on {type_name}` block"
        assert _direct_field(fragment.selection_set, "appliesOncePerCustomer") is not None, (
            f"{type_name}: missing appliesOncePerCustomer"
        )
        context_field = _direct_field(fragment.selection_set, "context")
        assert context_field is not None, f"{type_name}: missing context"
        assert context_field.selection_set is not None
        assert (
            _direct_inline_fragment(context_field.selection_set, "DiscountBuyerSelectionAll")
            is not None
        ), f"{type_name}: context missing ... on DiscountBuyerSelectionAll"
        assert (
            _direct_inline_fragment(context_field.selection_set, "DiscountCustomers") is not None
        ), f"{type_name}: context missing ... on DiscountCustomers"
        segments_fragment = _direct_inline_fragment(
            context_field.selection_set, "DiscountCustomerSegments"
        )
        assert segments_fragment is not None, (
            f"{type_name}: context missing ... on DiscountCustomerSegments"
        )
        segments_field = _direct_field(segments_fragment.selection_set, "segments")
        assert segments_field is not None, f"{type_name}: DiscountCustomerSegments missing segments"
        assert segments_field.selection_set is not None
        assert _direct_field(segments_field.selection_set, "id") is not None, (
            f"{type_name}: segments missing id"
        )
        assert _direct_field(segments_field.selection_set, "name") is not None, (
            f"{type_name}: segments missing name"
        )


# ---------- read operations (build vars + execute, return node list) ----------


def _discount_node(
    gid, title, status="ACTIVE", codes=None, percentage=None, ends_at=None, usage_limit=None
):
    discount = {
        "__typename": "DiscountCodeBasic",
        "title": title,
        "status": status,
        "endsAt": ends_at,
        "usageLimit": usage_limit,
        "codes": {"nodes": [{"code": c} for c in (codes or [title])]},
    }
    if percentage is not None:
        discount["customerGets"] = {
            "value": {"__typename": "DiscountPercentage", "percentage": percentage}
        }
    return {"id": f"gid://shopify/DiscountCodeNode/{gid}", "discount": discount}


def test_read_code_discounts_returns_nodes_and_uses_fixed_page_size():
    fc = FakeClient([{"discountNodes": {"nodes": [_discount_node("1", "Spring", percentage=0.2)]}}])
    out = ops.read_code_discounts(fc)
    assert out == [_discount_node("1", "Spring", percentage=0.2)]
    assert fc.calls[0][0] == q.GET_CODE_DISCOUNTS
    assert fc.calls[0][1] == {
        "first": ops.CODE_DISCOUNTS_PAGE_SIZE,
        "query": "method:code",
        "codesFirst": ops.DISCOUNT_CODES_PER_DISCOUNT_CAP,
    }
    assert ops.CODE_DISCOUNTS_PAGE_SIZE == 50
    assert ops.DISCOUNT_CODES_PER_DISCOUNT_CAP == 10


def test_read_code_discounts_empty_returns_empty_list():
    fc = FakeClient([{"discountNodes": {"nodes": []}}])
    assert ops.read_code_discounts(fc) == []


def test_read_code_discounts_missing_connection_returns_empty_list():
    """Defensive: a permissions-trimmed / shape-drifted response with no
    discountNodes connection yields an empty list rather than raising."""
    fc = FakeClient([{}])
    assert ops.read_code_discounts(fc) == []


# ---------- write operations (build input + execute, return raw result) -------


def test_create_discount_code_basic_wraps_input_and_returns_raw_result():
    raw = {
        "discountCodeBasicCreate": {
            "codeDiscountNode": {"id": "gid://shopify/DiscountCodeNode/1"},
            "userErrors": [],
        }
    }
    fc = FakeClient([raw])
    discount_input = {
        "title": "T",
        "code": "LAUNCH20",
        "customerGets": {"value": {"percentage": 0.2}, "items": {"all": True}},
    }
    out = ops.create_discount_code_basic(fc, discount_input)
    assert out is raw
    assert fc.calls[0][0] == q.CREATE_DISCOUNT_CODE_BASIC
    assert fc.calls[0][1] == {"input": discount_input}
