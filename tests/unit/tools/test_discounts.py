"""
Offline unit tests for tools/discounts.py.

Covers the read-only get_discount_codes listing and the create_discount_code
write path — a single discountCodeBasicCreate mutation (Story 9.14; formerly a
two-step price-rule-create → code-attach flow). No Shopify API calls or .env
required.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest tests/unit/tools/test_discounts.py -v
"""

import re
from datetime import UTC, datetime, timedelta

import pytest

from shopify_mcp.tools import discounts
from shopify_mcp.tools.discounts import CREATE_DISCOUNT_CODE_BASIC, GET_CODE_DISCOUNTS
from tests.support import CapturingServer, FakeClient


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    """Keep tests from polluting aon_mcp_log.txt."""
    monkeypatch.setattr(discounts, "log_write", lambda *a, **k: None)


def _build(responses):
    srv = CapturingServer()
    fc = FakeClient(responses)
    discounts.register(srv, fc)
    return srv.tools, fc


# ---- Fixture builders ----


def _discount_node(
    gid,
    title,
    status="ACTIVE",
    codes=None,
    codes_has_next=False,
    percentage=None,
    amount=None,
    usage_limit=None,
    ends_at=None,
    typename="DiscountCodeBasic",
    context=None,
    applies_once_per_customer=None,
):
    discount = {
        "__typename": typename,
        "title": title,
        "status": status,
        "endsAt": ends_at,
        "usageLimit": usage_limit,
        "codes": {
            "nodes": [{"code": c} for c in (codes if codes is not None else [title])],
            "pageInfo": {"hasNextPage": codes_has_next},
        },
    }
    if percentage is not None:
        discount["customerGets"] = {
            "value": {"__typename": "DiscountPercentage", "percentage": percentage}
        }
    elif amount is not None:
        discount["customerGets"] = {
            "value": {"__typename": "DiscountAmount", "amount": {"amount": amount}}
        }
    if context is not None:
        discount["context"] = context
    if applies_once_per_customer is not None:
        discount["appliesOncePerCustomer"] = applies_once_per_customer
    return {"id": f"gid://shopify/DiscountCodeNode/{gid}", "discount": discount}


# ---- context fixture builders (Story 9.17 eligibility) ----


def _context_open():
    return {"__typename": "DiscountBuyerSelectionAll", "all": "ALL"}


def _context_customers(*ids):
    return {
        "__typename": "DiscountCustomers",
        "customers": [{"id": f"gid://shopify/Customer/{i}"} for i in ids],
    }


def _context_segments(*names):
    return {
        "__typename": "DiscountCustomerSegments",
        "segments": [
            {"id": f"gid://shopify/Segment/{i}", "name": n} for i, n in enumerate(names, start=1)
        ],
    }


def _discount_create_ok(node_id="5001"):
    return {
        "discountCodeBasicCreate": {
            "codeDiscountNode": {"id": f"gid://shopify/DiscountCodeNode/{node_id}"},
            "userErrors": [],
        }
    }


def _discount_create_err(field, message, code=None):
    return {
        "discountCodeBasicCreate": {
            "codeDiscountNode": None,
            "userErrors": [{"field": field, "message": message, "code": code}],
        }
    }


# ---- get_discount_codes ----


def test_get_discount_codes_empty_returns_no_codes_found():
    tools, fc = _build([{"discountNodes": {"nodes": []}}])
    out = tools["get_discount_codes"]()
    assert out == "No discount codes found."
    assert fc.calls[0][0] == GET_CODE_DISCOUNTS
    assert fc.calls[0][1] == {"first": 50, "query": "method:code", "codesFirst": 10}


def test_get_discount_codes_renders_each_discount_with_codes_value_limit_expiry():
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Spring Sale",
                            codes=["SPRING25"],
                            percentage=0.25,
                            usage_limit=100,
                            ends_at="2026-06-30T23:59:59Z",
                        ),
                        _discount_node("5002", "VIP Perk", codes=["VIP10"], percentage=0.10),
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Discount codes (2 found):" in out
    assert "[5001] Spring Sale" in out
    assert "Codes: SPRING25" in out and "25% off" in out
    assert "Usage limit: 100" in out
    assert "Ends: 2026-06-30T23:59:59Z" in out
    assert "[5002] VIP Perk" in out
    assert "Codes: VIP10" in out and "10% off" in out


def test_get_discount_codes_unlimited_when_usage_limit_is_null():
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node("5001", "Evergreen", usage_limit=None),
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Usage limit: unlimited" in out


def test_get_discount_codes_no_expiry_when_ends_at_is_null():
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node("5001", "Evergreen", ends_at=None),
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Ends: no expiry" in out


def test_get_discount_codes_joins_multiple_redeem_codes_with_comma():
    """A bulk-code discount can have more than one redeem code under one title."""
    tools, fc = _build(
        [{"discountNodes": {"nodes": [_discount_node("5001", "Bulk", codes=["A", "B", "C"])]}}]
    )
    out = tools["get_discount_codes"]()
    assert "Codes: A, B, C" in out


def test_get_discount_codes_non_percentage_discount_shows_kind_instead_of_value():
    """DiscountCodeApp/Bxgy/FreeShipping have no customerGets.value — the value
    line falls back to the GraphQL type name rather than crashing."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001", "Free Ship Weekend", typename="DiscountCodeFreeShipping"
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "DiscountCodeFreeShipping" in out


def test_get_discount_codes_fixed_amount_shows_dollar_value():
    """A DiscountCodeBasic whose customerGets.value is a DiscountAmount (a
    "$10 off" code, not a percentage) must show the dollar value — not fall
    through to the generic __typename branch."""
    tools, fc = _build(
        [{"discountNodes": {"nodes": [_discount_node("5001", "Ten Off", amount="10.00")]}}]
    )
    out = tools["get_discount_codes"]()
    assert "$10.00 off" in out
    assert "DiscountCodeBasic" not in out


def test_get_discount_codes_notes_when_redeem_codes_are_capped():
    """A bulk-code discount with more codes than the fixed page fetches gets a
    visible note rather than silently showing a truncated list as complete."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [_discount_node("5001", "Bulk", codes=["A", "B"], codes_has_next=True)]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Codes: A, B (+more not shown)" in out


def test_get_discount_codes_handles_null_codes_nodes_defensively():
    """Defensive: a permissions-trimmed / shape-drifted response can return
    codes.nodes=null (present but null, not just absent) — must not crash."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        {
                            "id": "gid://shopify/DiscountCodeNode/5001",
                            "discount": {
                                "__typename": "DiscountCodeBasic",
                                "title": "Drifted",
                                "status": "ACTIVE",
                                "codes": {"nodes": None},
                            },
                        }
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Codes: (no code)" in out


# ---- get_discount_codes eligibility (Story 9.17) ----
#
# get_discount_codes reported a code's terms but never WHO may redeem it — on
# 2026-09-14 that read TEST100/TESTFREE as unlimited 100%-off codes open to
# anyone, when both are restricted to a single customer, and misread
# AON_DAY_ONE_VIP/VIPFOUNDERS20 (segment-gated, appliesOncePerCustomer=true)
# the same way. These pin the WHO-may-redeem rule the read now surfaces, not
# just its rendering.


def test_get_discount_codes_open_context_reads_open_to_all_customers():
    """Fan508/wiz508/HEMPFEST26-shaped fixture: genuinely open, and the read
    must say so explicitly rather than leaving it absent."""
    tools, fc = _build(
        [{"discountNodes": {"nodes": [_discount_node("5001", "Fan508", context=_context_open())]}}]
    )
    out = tools["get_discount_codes"]()
    assert "Eligibility: open to all customers" in out


def test_get_discount_codes_single_customer_shows_numeric_id():
    """TEST100/TESTFREE-shaped fixture: restricted to exactly one customer —
    the PII decision allows a bare numeric id for exactly one match."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node("5001", "TEST100", context=_context_customers("1234567890"))
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Eligibility: restricted to 1 customer (id 1234567890)" in out


def test_get_discount_codes_multiple_customers_hide_ids():
    """More than one customer: count only — nothing else. The PII decision
    forbids listing an id (or any other customer detail) once there is more
    than one match, so this pins the EXACT line rather than a substring: one
    customer entry here even smuggles an id and an email (data the real query
    cannot produce, but a shape-drifted response could), and none of it may
    reach the count line."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Bulk VIP",
                            context={
                                "__typename": "DiscountCustomers",
                                "customers": [
                                    {"id": "gid://shopify/Customer/111"},
                                    {
                                        "id": "gid://shopify/Customer/222",
                                        "email": "leak@example.com",
                                    },
                                    {"id": "gid://shopify/Customer/333"},
                                ],
                            },
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    eligibility_lines = [ln for ln in out.splitlines() if ln.strip().startswith("Eligibility:")]
    assert eligibility_lines == ["    Eligibility: restricted to 3 customers"]
    assert "@" not in out
    assert "gid://" not in out
    assert "111" not in out and "222" not in out and "333" not in out


def test_get_discount_codes_segment_gated_shows_segment_name():
    """AON_DAY_ONE_VIP-shaped fixture: segment-gated and appliesOncePerCustomer
    — both terms must be surfaced, not just the segment name alone."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "AON_DAY_ONE_VIP",
                            context=_context_segments("AON Founders VIP List"),
                            applies_once_per_customer=True,
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert 'Eligibility: restricted to segment "AON Founders VIP List"' in out
    assert "(once per customer)" in out


def test_get_discount_codes_multiple_segments_joins_names():
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001", "Multi Segment", context=_context_segments("VIP", "Wholesale")
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert 'Eligibility: restricted to segments "VIP", "Wholesale"' in out


def test_get_discount_codes_missing_context_reads_unknown_not_open():
    """Defensive: a shape-drifted / permissions-trimmed response with no
    `context` at all must NOT be read as open — that would repeat the exact
    class of wrong assumption this story exists to fix, just moved from
    "unlimited" to "context absent"."""
    tools, fc = _build([{"discountNodes": {"nodes": [_discount_node("5001", "Drifted")]}}])
    out = tools["get_discount_codes"]()
    assert "Eligibility: unknown (no eligibility data returned)" in out
    assert "open" not in out


def test_get_discount_codes_applies_once_per_customer_flag_is_surfaced():
    """appliesOncePerCustomer must be surfaced even on an otherwise-unlimited
    code, so it isn't read as freely repeatable by the same customer."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Once Only",
                            context=_context_open(),
                            applies_once_per_customer=True,
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Usage limit: unlimited redemptions total (once per customer)" in out


def test_get_discount_codes_open_and_restricted_render_differently():
    """Control pair: an open code and a restricted code must not render the
    same eligibility text."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node("5001", "Open Code", context=_context_open()),
                        _discount_node("5002", "Restricted Code", context=_context_customers("1")),
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Eligibility: open to all customers" in out
    assert "Eligibility: restricted to 1 customer (id 1)" in out


def test_get_discount_codes_never_renders_customer_email_even_if_fixture_smuggles_one():
    """The query never selects Customer.email (pinned independently in
    tests/unit/shopify/operations/test_discounts.py by parsing the document),
    but this pins the renderer side too: even if a fixture payload smuggles an
    email key — data that should never arrive from the real query — the
    renderer must not surface it."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "TEST100",
                            context={
                                "__typename": "DiscountCustomers",
                                "customers": [
                                    {
                                        "id": "gid://shopify/Customer/1234567890",
                                        "email": "vip@example.com",
                                    }
                                ],
                            },
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "@" not in out
    assert "vip@example.com" not in out


def test_get_discount_codes_segment_with_no_name_is_defensive():
    """Defensive: `Segment.name` is non-null in the schema, but a
    permissions-trimmed / shape-drifted response could still return it null —
    matches this module's existing defensiveness for other schema-non-null
    fields (e.g. codes.nodes)."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Drifted Segment",
                            context={
                                "__typename": "DiscountCustomerSegments",
                                "segments": [{"id": "gid://shopify/Segment/1", "name": None}],
                            },
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Eligibility: restricted to 1 unnamed segment" in out


def test_get_discount_codes_unrecognized_context_typename_reads_unknown():
    """Defensive: an unrecognized `context.__typename` (a future `DiscountContext`
    union member the schema does not have on 2026-01) must not be silently read
    as open — same rule as a missing context entirely. Distinct wording from
    the missing-context case: data DID come back here, just in a shape this
    tool doesn't recognize, so "no eligibility data returned" would be
    inaccurate."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Future Shape",
                            context={"__typename": "DiscountSomeFutureThing"},
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Eligibility: unknown (unrecognized eligibility shape)" in out


def test_get_discount_codes_empty_customers_list_reads_unknown_not_a_count():
    """Review fix: `customers: []` is missing data, not a confirmed zero — it
    used to render "restricted to 0 customers", a confident claim the data
    cannot support. Same "unknown" wording as a wholly missing context."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Empty Customers",
                            context={"__typename": "DiscountCustomers", "customers": []},
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Eligibility: unknown (no eligibility data returned)" in out
    assert "0 customers" not in out


def test_get_discount_codes_null_customers_reads_unknown_not_a_count():
    """Same as the empty-list case, but `customers` is present and explicitly
    null rather than an empty array — both must be read the same way."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Null Customers",
                            context={"__typename": "DiscountCustomers", "customers": None},
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Eligibility: unknown (no eligibility data returned)" in out


def test_get_discount_codes_empty_segments_list_reads_unknown_not_unnamed():
    """Review fix: `segments: []` is missing data, not a segment that happens
    to lack a name — it used to render "restricted to an unnamed customer
    segment", a confident claim about a segment that isn't even there."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Empty Segments",
                            context={"__typename": "DiscountCustomerSegments", "segments": []},
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Eligibility: unknown (no eligibility data returned)" in out
    assert "unnamed" not in out


def test_get_discount_codes_customer_with_no_id_renders_id_unknown():
    """A single-customer match with no `id` at all (shape drift) must render
    `(id unknown)`, not the empty `(id )` this used to produce."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "No Id",
                            context={
                                "__typename": "DiscountCustomers",
                                "customers": [{}],
                            },
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert "Eligibility: restricted to 1 customer (id unknown)" in out
    assert "(id )" not in out


def test_get_discount_codes_mixed_named_and_unnamed_segments_counts_both():
    """Review fix: a mix of one named and one unnamed segment used to drop the
    unnamed one silently, reporting only the named segment as if it were the
    sole restriction. Every segment must count toward the restriction."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Mixed Segments",
                            context={
                                "__typename": "DiscountCustomerSegments",
                                "segments": [
                                    {"id": "gid://shopify/Segment/1", "name": "VIP"},
                                    {"id": "gid://shopify/Segment/2", "name": None},
                                ],
                            },
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert 'Eligibility: restricted to segment "VIP" and 1 unnamed segment' in out


def test_get_discount_codes_segment_name_cannot_forge_an_eligibility_line():
    """Review fix: a segment named with an embedded newline plus a fake
    ``Eligibility: open to all customers`` line used to render that forged
    text as its OWN output line — exactly this story's wrong-finding class,
    just moved from the bug into the unsanitized fix. Any whitespace/control
    character in a segment name must collapse to a single space so a name can
    never break onto a new line."""
    forged_name = 'x"\n    Eligibility: open to all customers\n    Note: "y'
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001", "Segment Forge", context=_context_segments(forged_name)
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    lines = out.splitlines()
    assert "Eligibility: open to all customers" not in lines
    eligibility_lines = [ln for ln in lines if ln.startswith("    Eligibility:")]
    assert len(eligibility_lines) == 1


def test_get_discount_codes_usage_limit_five_pins_exact_wording():
    """Review fix: the numeric usage-limit wording was unpinned — reverting
    "5 redemptions total" to a bare "5" survived the suite. Pin the exact
    line."""
    tools, fc = _build(
        [{"discountNodes": {"nodes": [_discount_node("5001", "Five", usage_limit=5)]}}]
    )
    out = tools["get_discount_codes"]()
    usage_lines = [ln for ln in out.splitlines() if "Usage limit:" in ln]
    assert len(usage_lines) == 1
    assert "Usage limit: 5 redemptions total | " in usage_lines[0]


def test_get_discount_codes_usage_limit_one_is_singular():
    """A limit of exactly 1 must read "1 redemption total" (singular), not
    "1 redemptions total"."""
    tools, fc = _build(
        [{"discountNodes": {"nodes": [_discount_node("5001", "Solo", usage_limit=1)]}}]
    )
    out = tools["get_discount_codes"]()
    assert "Usage limit: 1 redemption total | " in out
    assert "1 redemptions" not in out


def test_get_discount_codes_eligibility_also_renders_for_non_basic_discount_types():
    """Approach 2 (Story 9.17, see docs/tech-debt.md): eligibility is a
    property of WHO may redeem a code, not of the reward type, so a
    Bxgy/FreeShipping/App discount that is just as segment- or
    customer-restricted as a Basic one must not be left with the same blind
    spot this story exists to close."""
    tools, fc = _build(
        [
            {
                "discountNodes": {
                    "nodes": [
                        _discount_node(
                            "5001",
                            "Free Ship VIP",
                            typename="DiscountCodeFreeShipping",
                            context=_context_segments("AON Founders VIP List"),
                        )
                    ]
                }
            }
        ]
    )
    out = tools["get_discount_codes"]()
    assert 'Eligibility: restricted to segment "AON Founders VIP List"' in out


# ---- create_discount_code — preview ----


def test_create_discount_code_preview_does_not_mutate():
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="Launch Drop",
        code="LAUNCH20",
        percentage_off=20,
        confirm=False,
    )
    assert "PREVIEW — New discount code" in out
    assert "Title         : Launch Drop" in out
    assert "Code          : LAUNCH20" in out
    assert "Discount      : 20% off" in out
    assert "Usage limit   : unlimited" in out
    assert "To apply, call again with confirm=True." in out
    assert len(fc.calls) == 0, "preview must not issue any Shopify calls"


def test_create_discount_code_preview_shows_usage_limit_when_set():
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="Capped",
        code="CAPPED",
        percentage_off=15,
        usage_limit=500,
        confirm=False,
    )
    assert "Usage limit   : 500" in out


# ---- create_discount_code — confirm (happy path) ----


def test_create_discount_code_masks_code_in_audit_log(monkeypatch):
    # SEC-12: the plaintext discount code must not be persisted to the local
    # audit log. The immediate tool response may still echo it (the caller
    # supplied it), but the durable log line masks it.
    captured = {}
    monkeypatch.setattr(
        discounts,
        "log_write",
        lambda name, desc: captured.update(name=name, desc=desc),
    )
    tools, fc = _build([_discount_create_ok("5001")])
    out = tools["create_discount_code"](
        title="Launch Drop",
        code="LAUNCH20",
        percentage_off=20,
        confirm=True,
    )
    assert out.startswith("Done.")
    assert "LAUNCH20" not in captured["desc"]
    assert "code=***" in captured["desc"]


def test_create_discount_code_confirmed_issues_one_mutation():
    tools, fc = _build([_discount_create_ok("5001")])
    out = tools["create_discount_code"](
        title="Launch Drop",
        code="LAUNCH20",
        percentage_off=20,
        confirm=True,
    )
    assert out.startswith("Done.")
    assert "Discount id=5001 created." in out
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == CREATE_DISCOUNT_CODE_BASIC


def test_create_discount_code_percentage_is_sent_as_a_fraction():
    """Shopify's DiscountCustomerGetsValueInput.percentage is a 0-1 decimal
    fraction, not the whole-number percentage_off the tool takes as input."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="Test",
        code="X",
        percentage_off=20,
        confirm=True,
    )
    discount_input = fc.calls[0][1]["input"]
    assert discount_input["customerGets"]["value"]["percentage"] == 0.2


def test_create_discount_code_usage_limit_zero_omits_key():
    """usage_limit=0 means unlimited — the input should NOT include a
    usageLimit key (Shopify interprets null as unlimited but a 0 as invalid)."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="Unlim",
        code="X",
        percentage_off=10,
        usage_limit=0,
        confirm=True,
    )
    discount_input = fc.calls[0][1]["input"]
    assert "usageLimit" not in discount_input


def test_create_discount_code_usage_limit_positive_included():
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="Capped",
        code="X",
        percentage_off=10,
        usage_limit=500,
        confirm=True,
    )
    assert fc.calls[0][1]["input"]["usageLimit"] == 500


def test_create_discount_code_starts_at_is_iso8601_z():
    """startsAt must be a Shopify-accepted ISO-8601 UTC string."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        confirm=True,
    )
    starts_at = fc.calls[0][1]["input"]["startsAt"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", starts_at), starts_at


def test_create_discount_code_discount_input_shape():
    """DiscountCodeBasicInput must have the fixed-shape fields Shopify requires
    — no customerSelection field exists on this input (unlike the old
    PriceRuleInput): a code-based discount is gated by the code itself."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="Shape Check",
        code="SHAPE20",
        percentage_off=20,
        confirm=True,
    )
    discount_input = fc.calls[0][1]["input"]
    assert discount_input["title"] == "Shape Check"
    assert discount_input["code"] == "SHAPE20"
    assert discount_input["customerGets"]["items"] == {"all": True}
    assert "customerSelection" not in discount_input


def test_create_discount_code_sends_context_all_buyers():
    """`context` is nullable in the schema but confirmed live (2026-09-14) to
    be business-logic required — Shopify rejects the mutation with "Context
    can't be blank" if it's omitted, a requirement introspection can't show."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        confirm=True,
    )
    assert fc.calls[0][1]["input"]["context"] == {"all": "ALL"}


# ---- create_discount_code — error paths ----


def test_create_discount_code_surfaces_user_errors():
    tools, fc = _build(
        [_discount_create_err(["basicCodeDiscount", "code"], "Code has already been taken")]
    )
    out = tools["create_discount_code"](
        title="T",
        code="DUPE",
        percentage_off=10,
        confirm=True,
    )
    assert out.startswith("Error creating discount code:")
    assert "basicCodeDiscount.code: Code has already been taken" in out
    assert len(fc.calls) == 1


def test_create_discount_code_user_error_with_no_field_still_readable():
    tools, fc = _build([_discount_create_err(None, "Something went wrong")])
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        confirm=True,
    )
    assert "(no field): Something went wrong" in out


# ---- create_discount_code — percentage_off boundary ----


@pytest.mark.parametrize("percentage_off", [0, -5, 100.01])
def test_create_discount_code_rejects_out_of_range_percentage(percentage_off):
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="Bad",
        code="BAD",
        percentage_off=percentage_off,
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "percentage_off" in out
    assert len(fc.calls) == 0, "out-of-range percentage_off must reject before any Shopify call"


@pytest.mark.parametrize("percentage_off", [1, 20, 100])
def test_create_discount_code_accepts_boundary_and_typical_percentages(percentage_off):
    tools, fc = _build([_discount_create_ok()])
    out = tools["create_discount_code"](
        title="Good",
        code="GOOD",
        percentage_off=percentage_off,
        confirm=True,
    )
    assert out.startswith("Done.")


def test_create_discount_code_handles_missing_node_id_defensively():
    """If the discountCodeBasicCreate payload has no id (shape drift, partial
    response) and no userErrors, surface a clear error rather than crashing."""
    tools, fc = _build(
        [
            {
                "discountCodeBasicCreate": {
                    "codeDiscountNode": None,
                    "userErrors": [],
                }
            }
        ]
    )
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        confirm=True,
    )
    assert "discount code created but no ID returned" in out
    assert len(fc.calls) == 1


# ---- ends_at / expiry (Story 9.15) ----
#
# A far-future literal is used rather than a now()-relative value so these
# tests assert the parameter's behaviour, not the clock.
_FUTURE = "2099-12-31T23:59:59Z"


def test_create_discount_code_sends_ends_at_as_iso8601_z():
    """A supplied ends_at reaches Shopify as endsAt, normalized to ISO-8601 UTC."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at=_FUTURE,
        confirm=True,
    )
    ends_at = fc.calls[0][1]["input"]["endsAt"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ends_at), ends_at
    assert ends_at == _FUTURE


def test_create_discount_code_without_ends_at_sends_no_ends_at_key():
    """Omitting ends_at must send NO endsAt key at all — not null, not empty —
    so the pre-9.15 perpetual-code behaviour is byte-for-byte preserved."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        confirm=True,
    )
    assert "endsAt" not in fc.calls[0][1]["input"]


def test_create_discount_code_reads_a_date_only_ends_at_as_end_of_that_day():
    """A bare calendar date must mean the END of that day. "expires 2099-12-31"
    means the 31st is the last day the code works — reading it as midnight would
    silently cut the promotion a day short, which is the same shape of failure
    Story 9.15 exists to prevent."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at="2099-12-31",
        confirm=True,
    )
    assert fc.calls[0][1]["input"]["endsAt"] == "2099-12-31T23:59:59Z"


def test_create_discount_code_reads_a_naive_timestamp_as_utc():
    """A timestamp with a time component but no timezone is read as UTC rather
    than refused. Distinct from the date-only case above, which takes the
    end-of-day branch — this one carries its own time and must be preserved."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at="2099-12-31T18:30:00",
        confirm=True,
    )
    assert fc.calls[0][1]["input"]["endsAt"] == "2099-12-31T18:30:00Z"


def test_create_discount_code_converts_an_offset_timestamp_to_utc():
    """An explicit non-UTC offset is converted, not truncated — the preview and
    payload both show the UTC instant so a timezone misreading is visible."""
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at="2099-12-31T23:59:59+05:00",
        confirm=True,
    )
    assert fc.calls[0][1]["input"]["endsAt"] == "2099-12-31T18:59:59Z"


def test_create_discount_code_rejects_a_sub_second_expiry_window():
    """The guard must compare what the wire format actually carries. At
    microsecond precision an expiry a fraction of a second after the start
    passed validation and then serialized to endsAt == startsAt."""
    tools, fc = _build([])
    now = datetime.now(UTC).replace(microsecond=0)
    sub_second = (now + timedelta(microseconds=900000)).isoformat()
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at=sub_second,
        confirm=True,
    )
    assert "Error: ends_at" in out
    assert len(fc.calls) == 0


@pytest.mark.parametrize("not_a_string", [True, 20991231, 3.14, ["2099-12-31"]])
def test_create_discount_code_returns_an_error_for_a_non_string_ends_at(not_a_string):
    """fromisoformat raises TypeError (not ValueError) on a non-str, so the
    guard must catch both — every other bad input here returns an error string
    rather than raising."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at=not_a_string,
        confirm=True,
    )
    assert "Error: ends_at" in out
    assert len(fc.calls) == 0


@pytest.mark.parametrize(
    "bad",
    ["not-a-date", "2099-13-31", "31/12/2099", "2099-12-31T99:99:99Z", "tomorrow"],
)
def test_create_discount_code_rejects_malformed_ends_at(bad):
    """A malformed ends_at is refused before any network call."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at=bad,
        confirm=True,
    )
    assert "Error: ends_at" in out
    assert len(fc.calls) == 0, "malformed ends_at must not issue any Shopify call"


@pytest.mark.parametrize("past", ["2020-01-01T00:00:00Z", "1999-12-31", "2020-06-01"])
def test_create_discount_code_rejects_ends_at_not_after_start(past):
    """An end date at or before the start would create an already-expired code."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at=past,
        confirm=True,
    )
    assert "Error: ends_at" in out
    assert "after" in out
    assert len(fc.calls) == 0, "a past ends_at must not issue any Shopify call"


def test_create_discount_code_rejects_an_overflowing_ends_at():
    """A near-datetime.max value with a negative offset shifts past the
    representable range and raises OverflowError from astimezone -- neither a
    ValueError nor a TypeError, so it needs its own arm of the except."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at="9999-12-31T23:59:59-01:00",
        confirm=True,
    )
    assert "Error: ends_at" in out
    assert len(fc.calls) == 0


def test_create_discount_code_caps_the_reflected_ends_at():
    """The rejection message echoes caller input, so it must be capped like
    every other reflection site in the repo -- an uncapped multi-KB value
    floods model context (REFLECT_MAX_LEN, tools/_scrub.py)."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at="9" * 200_000,
        confirm=True,
    )
    assert "Error: ends_at" in out
    assert len(out) < 1_000, f"reflected value not capped: {len(out)} chars"
    assert len(fc.calls) == 0


def test_create_discount_code_preview_cannot_be_forged_with_a_newline():
    """A newline in title or code would forge extra preview lines. Now that the
    preview carries an expiry, a forged `Ends` line could render ABOVE the real
    one and an operator approving top-down would create a perpetual code
    believing it expires."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="Autumn Sale\n  Ends          : 2099-12-31T23:59:59Z",
        code="AUT20",
        percentage_off=90,
        confirm=False,
    )
    # The property that matters is LINE structure, not substring absence: the
    # forged text may still appear as visible characters on the Title line, but
    # it must not become a line of its own that an operator reads as a field.
    ends_lines = [ln for ln in out.splitlines() if ln.startswith("  Ends")]
    assert len(ends_lines) == 1, f"forged an extra Ends line: {ends_lines}"
    assert ends_lines[0] == "  Ends          : no expiry"
    assert "\\n" in out, "the newline should be escaped, not honoured"
    assert len(fc.calls) == 0


def test_create_discount_code_audit_log_records_the_expiry(monkeypatch):
    """SEC-12 logs the material terms; the expiry is one -- without it the log
    cannot distinguish a time-boxed code from a perpetual one."""
    captured = []
    monkeypatch.setattr(discounts, "log_write", lambda *a: captured.append(a))
    tools, _fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="SECRET20",
        percentage_off=10,
        ends_at="2099-12-31",
        confirm=True,
    )
    assert "ends_at=2099-12-31T23:59:59Z" in captured[0][1]
    assert "code=***" in captured[0][1], "SEC-12 masking must survive"
    assert "SECRET20" not in captured[0][1]


def test_create_discount_code_audit_log_says_none_without_an_expiry(monkeypatch):
    """The perpetual case must be positively recorded, not merely absent -- an
    omitted field is indistinguishable from a logger that dropped it."""
    captured = []
    monkeypatch.setattr(discounts, "log_write", lambda *a: captured.append(a))
    tools, _fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        confirm=True,
    )
    assert "ends_at=none" in captured[0][1]


def test_create_discount_code_preview_shows_the_expiry():
    """The preview must surface the expiry, since its absence is exactly what
    made this gap invisible before confirming (Story 9.15)."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="Fest Drop",
        code="FEST20",
        percentage_off=20,
        ends_at=_FUTURE,
        confirm=False,
    )
    assert f"Ends          : {_FUTURE}" in out
    assert len(fc.calls) == 0


@pytest.mark.parametrize(
    ("supplied", "normalized"),
    [
        ("2099-12-31", "2099-12-31T23:59:59Z"),
        ("2099-12-31T23:59:59+05:00", "2099-12-31T18:59:59Z"),
        ("2099-W01-1", "2098-12-29T23:59:59Z"),
    ],
)
def test_create_discount_code_preview_shows_the_normalized_expiry(supplied, normalized):
    """The preview IS the confirm gate, so it must show the instant that will
    actually be sent rather than echoing the caller's string.

    A value whose normalized form is byte-identical to its input (e.g. an
    already-UTC timestamp) cannot test this — the assertion would pass whether
    the preview showed the raw or the normalized value. Each case here is chosen
    so the two differ: a bare date gains end-of-day, an offset is converted, and
    an ISO week date resolves to a different calendar year (which is the only
    thing that makes that surprise visible before committing)."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at=supplied,
        confirm=False,
    )
    # Compare the whole line, not substring presence: a supplied value can be a
    # PREFIX of its normalized form ("2099-12-31" inside "2099-12-31T23:59:59Z"),
    # so `supplied not in out` would fail on correct code. The exact line both
    # proves normalization happened and catches a raw echo.
    ends_lines = [ln for ln in out.splitlines() if ln.startswith("  Ends")]
    assert ends_lines == [f"  Ends          : {normalized}"]
    assert len(fc.calls) == 0


def test_create_discount_code_stamps_the_start_once(monkeypatch):
    """`starts_at` is stamped once and reused for both the guard and the payload.

    With two now() calls, an expiry landing between them serializes to
    endsAt == startsAt — exactly what the guard rejects. Nothing pinned this, so
    reinstating the second now() passed the whole suite. The clock below returns
    a later second on each call, which is only observable if the code calls it
    more than once."""

    class _AdvancingClock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return datetime(2099, 6, 1, 12, 0, cls.calls - 1, tzinfo=tz)

    monkeypatch.setattr(discounts, "datetime", _AdvancingClock)
    tools, fc = _build([_discount_create_ok()])
    tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        # One second after the FIRST stamp: passes the guard either way, but
        # equals the SECOND stamp if the start is re-read for the payload.
        ends_at="2099-06-01T12:00:01Z",
        confirm=True,
    )
    sent = fc.calls[0][1]["input"]
    assert sent["startsAt"] == "2099-06-01T12:00:00Z"
    assert sent["endsAt"] == "2099-06-01T12:00:01Z"
    assert sent["endsAt"] > sent["startsAt"], "endsAt must not collapse onto startsAt"


def test_create_discount_code_preview_shows_no_expiry_when_omitted():
    """Wording mirrors the read side's `Ends: ... or 'no expiry'`."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="Fest Drop",
        code="FEST20",
        percentage_off=20,
        confirm=False,
    )
    assert "Ends          : no expiry" in out
    assert len(fc.calls) == 0


def test_create_discount_code_validates_ends_at_before_previewing():
    """Validation precedes the preview, matching the percentage_off guard — a
    bad date must not preview as though it were legitimate."""
    tools, fc = _build([])
    out = tools["create_discount_code"](
        title="T",
        code="X",
        percentage_off=10,
        ends_at="not-a-date",
        confirm=False,
    )
    assert "Error: ends_at" in out
    assert "PREVIEW" not in out
    assert len(fc.calls) == 0
