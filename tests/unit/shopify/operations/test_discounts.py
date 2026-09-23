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

from graphql import FieldNode, parse

from shopify_mcp.shopify.operations import discounts as ops
from shopify_mcp.shopify.queries import discounts as q
from tests.support import FakeClient

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


def _selected_field_names(document_text: str) -> set[str]:
    """Every field name selected anywhere in a document, found by walking the
    parsed AST (graphql-core) rather than string-matching or identity-
    comparing the query text — see feedback_pin_graphql_by_parsing: query
    assertions must survive the text changing while the suite stays green."""
    document = parse(document_text)
    names: set[str] = set()

    def walk(selection_set):
        if selection_set is None:
            return
        for selection in selection_set.selections:
            if isinstance(selection, FieldNode):
                names.add(selection.name.value)
            walk(getattr(selection, "selection_set", None))

    for definition in document.definitions:
        walk(getattr(definition, "selection_set", None))
    return names


def test_get_code_discounts_query_never_selects_email():
    """PII guard (Story 9.17): the eligibility selection this story adds must
    never reach `email` — Customer.email is itself deprecated and this tool
    must never fetch or print a customer's email. Selecting `customers { id }`
    only means data that never arrives cannot leak."""
    assert "email" not in _selected_field_names(q.GET_CODE_DISCOUNTS)


def test_get_code_discounts_selects_eligibility_fields_on_every_discount_type():
    """Approach 2 (Story 9.17, see docs/tech-debt.md): eligibility is a
    property of WHO may redeem the code, not of the reward type, so it is
    selected identically on all four `Discount` union members rather than
    only `DiscountCodeBasic`."""
    assert q.GET_CODE_DISCOUNTS.count("appliesOncePerCustomer") == 4
    assert q.GET_CODE_DISCOUNTS.count("... on DiscountBuyerSelectionAll") == 4
    assert q.GET_CODE_DISCOUNTS.count("... on DiscountCustomers") == 4
    assert q.GET_CODE_DISCOUNTS.count("... on DiscountCustomerSegments") == 4


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
