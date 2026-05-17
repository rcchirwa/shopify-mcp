"""
Offline unit tests for tools/catalog_hygiene.py.

Wave 1 (Story 9.3) ships `update_product_pricing`. These tests pin:
  - the skeleton contract (register is callable, returns the expected tool)
  - input validation (whole-call reject on any invalid entry, including
    duplicate variantIds and whitespace handling)
  - variant ID resolution against the pre-fetched variants list (numeric /
    GID / SKU; ambiguous SKU; unknown SKU)
  - preview path (confirm=False — no mutation call)
  - idempotent fast-path (all targets already at target → no mutation)
  - mutation path (variants_input shape, userErrors mapping with dotted paths)
  - edge cases (compareAtPrice clear, at-cap warning, variant ID not on
    product slipping past numeric/GID short-circuit)

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_catalog_hygiene_offline.py -v
"""

import json
import re
from typing import Any

import pytest

from _testing import CapturingServer, FakeClient
from tools import catalog_hygiene
from tools.catalog_hygiene import (
    GET_PRODUCT_BY_HANDLE_MIN,
    GET_PRODUCT_CATEGORY,
    GET_PRODUCT_TYPE,
    GET_PRODUCT_TYPE_BY_HANDLE,
    GET_PRODUCT_VENDOR,
    GET_PRODUCT_VENDOR_BY_HANDLE,
    PRODUCT_TYPE_MAX_LEN,
    TAXONOMY_SEARCH,
    UPDATE_PRODUCT_CATEGORY,
    UPDATE_PRODUCT_TYPE,
    UPDATE_PRODUCT_VENDOR,
    VENDOR_MAX_LEN,
)


@pytest.fixture(autouse=True)
def _no_log_write(request, monkeypatch):
    """Keep tests from polluting aon_mcp_log.txt.

    `tools.catalog_hygiene` does `from tools._log import log_write`, so the
    callable lives at the `tools.catalog_hygiene.log_write` attribute. Patch
    the call-site module (not `tools._log`) or the binding won't intercept.
    Shared by 9.3 (update_product_pricing) and 9.1 (update_product_category).

    Scoped to tool-invoking tests — the Wave 0 contract tests (which only
    call `register()`) skip the patch since `log_write` is never reached.
    """
    if request.node.name.startswith("test_register_"):
        return
    monkeypatch.setattr(catalog_hygiene, "log_write", lambda *_a, **_kw: None)


def _build(responses):
    srv = CapturingServer()
    fc = FakeClient(responses)
    catalog_hygiene.register(srv, fc)
    return srv.tools, fc


_TAIL_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def _parse_tail(out: str) -> dict:
    """Extract and parse the fenced JSON tail emitted by every tool return.

    Spec amendment at shopify-aon-mcp-catalog-tools-spec.md:48 — every return
    is human-readable head + ```json``` tail. Tests use this to assert on the
    machine-readable contract independently of the head's wording.
    """
    match = _TAIL_RE.search(out)
    assert match is not None, f"No ```json``` tail found in output:\n{out}"
    return json.loads(match.group(1))


# Shared response fragments ----------------------------------------------------


def _read_response(variants=None, *, title="Test Product"):
    """One-read fixture — same query now backs SKU resolution AND old→new diff."""
    return {
        "product": {
            "id": "gid://shopify/Product/100",
            "title": title,
            "variants": {
                "nodes": variants
                or [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "39.99",
                        "compareAtPrice": "50.00",
                    },
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "SKU-B",
                        "price": "39.99",
                        "compareAtPrice": None,
                    },
                ],
            },
        }
    }


def _mutation_ok(variants):
    return {
        "productVariantsBulkUpdate": {
            "product": {"id": "gid://shopify/Product/100"},
            "productVariants": variants,
            "userErrors": [],
        }
    }


# Skeleton contract ------------------------------------------------------------


def test_register_is_callable_and_returns_none():
    srv = CapturingServer()
    fc = FakeClient([])
    assert catalog_hygiene.register(srv, fc) is None


def test_register_adds_expected_tools():
    # Pins the current tool set. Future Stories 9.4-9.5 will grow this;
    # update the assertion alongside each one.
    srv = CapturingServer()
    fc = FakeClient([])
    catalog_hygiene.register(srv, fc)
    assert set(srv.tools.keys()) == {
        "update_product_pricing",
        "update_product_category",
        "update_product_vendor",
        "update_product_type",
        "update_variant_image_binding",
        "set_product_metafields",
        "delete_product_metafields",
        "get_product_metafields",
        "update_product_options",
    }
    assert fc.calls == []


# Validation rejects — no network call ----------------------------------------


@pytest.mark.parametrize(
    "variants,fragment",
    [
        ([], "variants must be a non-empty list"),
        ("not-a-list", "variants must be a non-empty list"),
        ([{"price": "49.99"}], "variantId is required"),
        ([{"variantId": "  ", "price": "49.99"}], "variantId is required"),
        ([{"variantId": "201"}], "must supply price or compareAtPrice"),
        ([{"variantId": "201", "price": "-1.00"}], "is not a positive decimal"),
        ([{"variantId": "201", "price": "abc"}], "is not a positive decimal"),
        ([{"variantId": "201", "price": "0"}], "is not a positive decimal"),
        ([{"variantId": "201", "price": "0.00"}], "is not a positive decimal"),
        ([{"variantId": "201", "price": "49.999"}], "more than 2 decimal places"),
        ([{"variantId": "201", "price": ""}], "non-empty string"),
        ([{"variantId": "201", "price": 49.99}], "is not a string"),
        ([{"variantId": "201", "compareAtPrice": "-1.00"}], "is not a positive decimal"),
        ([{"variantId": "201", "compareAtPrice": "NaN"}], "is not a positive decimal"),
        (["not-a-dict"], "variants[0] must be an object"),
        (
            [
                {"variantId": "201", "price": "10.00"},
                {"variantId": "201", "compareAtPrice": "20.00"},
            ],
            "is a duplicate",
        ),
        # Whitespace-normalized duplicates: " 201 " and "201" both trim to "201".
        (
            [
                {"variantId": " 201 ", "price": "10.00"},
                {"variantId": "201", "compareAtPrice": "20.00"},
            ],
            "is a duplicate",
        ),
    ],
)
def test_validation_rejects(variants, fragment):
    tools, fc = _build([])
    out = tools["update_product_pricing"]("100", variants=variants)
    assert out.startswith("Error:")
    assert fragment in out
    assert fc.calls == []  # No network call before validation passes.


# Whitespace handling pins ----------------------------------------------------


def test_variant_id_with_whitespace_is_trimmed_before_resolution():
    # "  SKU-A  " trims to "SKU-A" then matches the pre-fetched variants list.
    tools, fc = _build(
        [
            _read_response(),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "49.99",
                        "compareAtPrice": "50.00",
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "  SKU-A  ", "price": "49.99"}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    # Mutation targets the resolved GID.
    assert fc.calls[1][1]["variants"][0]["id"] == "gid://shopify/ProductVariant/201"


def test_price_with_surrounding_whitespace_is_validated_and_stripped():
    tools, fc = _build(
        [
            _read_response(),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "49.99",
                        "compareAtPrice": "50.00",
                    }
                ]
            ),
        ]
    )
    tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "  49.99  "}],
        confirm=True,
    )
    # Stripped value reaches Shopify, not the whitespace-padded original.
    assert fc.calls[1][1]["variants"][0]["price"] == "49.99"


# Resolver error pass-through (in-memory) -------------------------------------


def test_unknown_sku_surfaces_resolver_error():
    tools, fc = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "SKU-MISSING", "price": "49.99"}],
    )
    assert out.startswith("Error: No variant on product")
    # Only the pricing read fires; no separate resolver query.
    assert len(fc.calls) == 1


def test_ambiguous_sku_surfaces_resolver_error():
    tools, fc = _build(
        [
            _read_response(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "DUPE",
                        "price": "10.00",
                        "compareAtPrice": None,
                    },
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "DUPE",
                        "price": "10.00",
                        "compareAtPrice": None,
                    },
                ]
            )
        ]
    )
    out = tools["update_product_pricing"]("100", variants=[{"variantId": "DUPE", "price": "49.99"}])
    assert "matches multiple variants" in out
    assert len(fc.calls) == 1


def test_product_not_found_surfaces_consistent_message():
    # Whether the caller passes a numeric ID or a SKU, the pricing read is
    # the first query that runs and produces the "No product found" message.
    tools, fc = _build([{"product": None}])
    out = tools["update_product_pricing"](
        "100", variants=[{"variantId": "SKU-A", "price": "49.99"}]
    )
    assert "No product found with id 100" in out


def test_product_not_found_for_numeric_path():
    tools, fc = _build([{"product": None}])
    out = tools["update_product_pricing"]("100", variants=[{"variantId": "201", "price": "49.99"}])
    assert "No product found with id 100" in out


# Preview path (confirm=False) -------------------------------------------------


def test_preview_path_no_mutation_call():
    tools, fc = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "49.99", "compareAtPrice": "65.00"},
        ],
    )
    assert "PREVIEW — Product pricing update" in out
    assert "Test Product" in out
    assert "price: 39.99 → 49.99" in out
    assert "compareAtPrice: 50.00 → 65.00" in out
    assert "confirm=True" in out  # with_confirm_hint footer
    # One read only; no mutation.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == catalog_hygiene.GET_PRODUCT_VARIANTS_FOR_PRICING


def test_preview_shows_cleared_compare_at():
    tools, fc = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "compareAtPrice": None}],
    )
    assert "compareAtPrice: 50.00 → (cleared)" in out


def test_preview_renders_none_old_value_as_none_token():
    # Variant 202 has compareAtPrice=None; setting it to a value should show
    # "(none) → 65.00".
    tools, fc = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "202", "compareAtPrice": "65.00"}],
    )
    assert "compareAtPrice: (none) → 65.00" in out


def test_preview_marks_already_at_target():
    tools, fc = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "39.99", "compareAtPrice": "50.00"},
        ],
    )
    assert "(already at target)" in out


# Mutation path (confirm=True) -------------------------------------------------


def test_confirm_path_sends_correct_mutation_input():
    tools, fc = _build(
        [
            _read_response(),
            _mutation_ok(
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
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "49.99", "compareAtPrice": "65.00"},
        ],
        confirm=True,
    )
    assert out.startswith("CONFIRMED — Product pricing update")
    assert "price: 49.99" in out
    assert "compareAtPrice: 65.00" in out
    mutation_call = fc.calls[1]
    assert mutation_call[0] == catalog_hygiene.UPDATE_PRODUCT_VARIANTS_PRICING
    assert mutation_call[1] == {
        "productId": "gid://shopify/Product/100",
        "variants": [
            {
                "id": "gid://shopify/ProductVariant/201",
                "price": "49.99",
                "compareAtPrice": "65.00",
            }
        ],
    }


def test_confirm_path_omits_unsupplied_fields_in_payload():
    # Only price supplied — compareAtPrice key MUST be absent from the payload
    # (otherwise Shopify would interpret a missing key as "no change", which is
    # what we want, vs. explicit null which clears).
    tools, fc = _build(
        [
            _read_response(),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "49.99",
                        "compareAtPrice": "50.00",
                    }
                ]
            ),
        ]
    )
    tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99"}],
        confirm=True,
    )
    assert fc.calls[1][1]["variants"] == [
        {"id": "gid://shopify/ProductVariant/201", "price": "49.99"}
    ]


def test_confirm_path_explicit_null_clears_compare_at():
    tools, fc = _build(
        [
            _read_response(),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "39.99",
                        "compareAtPrice": None,
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "compareAtPrice": None}],
        confirm=True,
    )
    # Payload includes the key with literal None — Shopify treats this as clear.
    assert fc.calls[1][1]["variants"] == [
        {"id": "gid://shopify/ProductVariant/201", "compareAtPrice": None}
    ]
    assert "compareAtPrice: (cleared)" in out


def test_confirm_path_handles_empty_returned_variants():
    # If Shopify's mutation response returns no productVariants, the
    # CONFIRMED block shows the "(none returned)" placeholder.
    tools, fc = _build(
        [
            _read_response(),
            {
                "productVariantsBulkUpdate": {
                    "product": {"id": "gid://shopify/Product/100"},
                    "productVariants": [],
                    "userErrors": [],
                }
            },
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99"}],
        confirm=True,
    )
    assert "(none returned)" in out


def test_confirm_path_renders_sku_none_in_confirmed_block():
    tools, fc = _build(
        [
            _read_response(),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": None,
                        "price": "49.99",
                        "compareAtPrice": None,
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99"}],
        confirm=True,
    )
    assert "(no SKU)" in out
    # Caller didn't pass compareAtPrice → head shows "(unchanged)", not
    # "(cleared)". See test_confirm_summary_unchanged_compare_at_price_*
    # below for the regression coverage of that distinction.
    assert "compareAtPrice: (unchanged)" in out
    assert "(cleared)" not in out


# Regression — copy-only bug surfaced during 9.3 integration smoke testing
# on 2026-05-12. The post-confirm head was reading `compareAtPrice` from the
# mutation response (which echoes current state, not the diff) and labeling
# null as "(cleared)", even when the caller never passed compareAtPrice and
# the variant already had compareAtPrice=null on its baseline.


def test_confirm_summary_unchanged_compare_at_price_null_is_not_cleared():
    # Scenario A: variant has compareAtPrice=null on baseline. Caller updates
    # `price` only. Head must NOT say "(cleared)" — the caller didn't clear
    # anything; the field was already null.
    tools, fc = _build(
        [
            _read_response(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "59.99",
                        "compareAtPrice": None,
                    }
                ]
            ),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "63.93",
                        "compareAtPrice": None,  # Shopify echoes current state.
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "63.93"}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED — Product pricing update")
    assert "price: 63.93" in out
    # The bug printed "(cleared)" here. Post-fix, the head shows the caller-
    # neutral "(unchanged)" token — caller didn't touch compareAtPrice.
    assert "(cleared)" not in out
    assert "compareAtPrice: (unchanged)" in out


def test_confirm_summary_unchanged_compare_at_price_with_value_shows_unchanged():
    # Scenario B (stronger): Shopify's mutation response echoes a non-null
    # compareAtPrice="10.00", but the caller still only updated `price`. The
    # head must surface the value alongside an "(unchanged)" label so the
    # reader can see current state without mistaking it for a diff.
    tools, fc = _build(
        [
            _read_response(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "59.99",
                        "compareAtPrice": "10.00",
                    }
                ]
            ),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "63.93",
                        "compareAtPrice": "10.00",  # echo of current state.
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "63.93"}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED — Product pricing update")
    assert "price: 63.93" in out
    assert "(cleared)" not in out
    assert "compareAtPrice: 10.00 (unchanged)" in out


def test_confirm_summary_explicit_clear_still_labelled_cleared():
    # Counter-test: when the caller DOES pass compareAtPrice=None, the head
    # must still say "(cleared)". This pins the kept half of the labelling
    # contract so a future refactor doesn't regress in the other direction.
    tools, fc = _build(
        [
            _read_response(),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "39.99",
                        "compareAtPrice": None,
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "compareAtPrice": None}],
        confirm=True,
    )
    assert "compareAtPrice: (cleared)" in out
    assert "(unchanged)" not in out


def test_confirm_summary_explicit_value_echoes_caller_value():
    # Counter-test: caller passes compareAtPrice="65.00" → head shows the
    # caller's value, not "(unchanged)".
    tools, fc = _build(
        [
            _read_response(),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "39.99",
                        "compareAtPrice": "65.00",
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "compareAtPrice": "65.00"}],
        confirm=True,
    )
    assert "compareAtPrice: 65.00" in out
    assert "(unchanged)" not in out
    assert "(cleared)" not in out


def test_confirm_summary_mixed_batch_keys_intent_per_variant():
    # Per-GID intent-map test: two variants in one call, one with
    # compareAtPrice explicitly passed and one without. Proves the intent
    # dict matches by GID (not by position/ordering), so a future refactor
    # that breaks the keying would surface here even when single-variant
    # tests all pass.
    tools, fc = _build(
        [
            _read_response(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "59.99",
                        "compareAtPrice": "10.00",
                    },
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "SKU-B",
                        "price": "29.99",
                        "compareAtPrice": None,
                    },
                ]
            ),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "63.93",
                        "compareAtPrice": "10.00",  # echo — caller didn't touch.
                    },
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "SKU-B",
                        "price": "29.99",
                        "compareAtPrice": "12.00",  # explicit set by caller.
                    },
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "63.93"},  # untouched compareAtPrice
            {"variantId": "202", "compareAtPrice": "12.00"},  # explicit
        ],
        confirm=True,
    )
    assert out.startswith("CONFIRMED — Product pricing update")
    # SKU-A: caller didn't touch compareAtPrice → "<current> (unchanged)".
    assert "SKU: SKU-A — price: 63.93 — compareAtPrice: 10.00 (unchanged)" in out
    # SKU-B: caller explicitly set 12.00 → echo caller's value (no label).
    assert "SKU: SKU-B — price: 29.99 — compareAtPrice: 12.00" in out
    assert "(cleared)" not in out
    # SKU-B's line must NOT be labelled "(unchanged)" — its presence would
    # mean the intent map wasn't matching SKU-B's GID.
    sku_b_line = next(line for line in out.splitlines() if "SKU-B" in line)
    assert "(unchanged)" not in sku_b_line


# Idempotent fast-path ---------------------------------------------------------


def test_idempotent_no_op_skips_mutation():
    tools, fc = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "39.99", "compareAtPrice": "50.00"},
        ],
        confirm=True,
    )
    assert "(no-op)" in out
    assert "all already at target values" in out
    # Only the read query fired — no mutation call.
    assert len(fc.calls) == 1


def test_idempotent_no_op_recognises_compare_at_already_cleared():
    # Variant 202 has compareAtPrice=None already; requesting clear is no-op.
    tools, fc = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "202", "compareAtPrice": None}],
        confirm=True,
    )
    assert "(no-op)" in out
    assert len(fc.calls) == 1


def test_idempotent_recognises_equivalent_decimal_strings():
    # 39.99 == 39.990 — decimal comparison must treat these as equal.
    tools, fc = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "39.99"}],
        confirm=True,
    )
    assert "(no-op)" in out


# SKU resolution + mutation ----------------------------------------------------


def test_sku_path_reads_once_then_mutates():
    tools, fc = _build(
        [
            _read_response(),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "49.99",
                        "compareAtPrice": "50.00",
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "SKU-A", "price": "49.99"}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    # Single read + single mutation — no extra resolver round-trip.
    assert len(fc.calls) == 2
    assert fc.calls[0][0] == catalog_hygiene.GET_PRODUCT_VARIANTS_FOR_PRICING
    assert fc.calls[1][1]["variants"][0]["id"] == "gid://shopify/ProductVariant/201"


def test_numeric_and_sku_pointing_at_same_variant_rejected():
    # "201" and "SKU-A" both resolve to ProductVariant/201. Post-resolution
    # dedup catches this so we don't send a redundant payload to Shopify.
    tools, fc = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "49.99"},
            {"variantId": "SKU-A", "compareAtPrice": "65.00"},
        ],
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "resolves to the same variant" in out
    # Read fired (resolution needs the variants list); no mutation.
    assert len(fc.calls) == 1


def test_three_distinct_variants_with_mixed_forms_single_read():
    # Three distinct variants, mixed identifier forms — single read only.
    tools, fc = _build(
        [
            _read_response(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "10.00",
                        "compareAtPrice": None,
                    },
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "SKU-B",
                        "price": "10.00",
                        "compareAtPrice": None,
                    },
                    {
                        "id": "gid://shopify/ProductVariant/203",
                        "sku": "SKU-C",
                        "price": "10.00",
                        "compareAtPrice": None,
                    },
                ]
            ),
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/201",
                        "sku": "SKU-A",
                        "price": "49.99",
                        "compareAtPrice": None,
                    },
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "SKU-B",
                        "price": "49.99",
                        "compareAtPrice": None,
                    },
                    {
                        "id": "gid://shopify/ProductVariant/203",
                        "sku": "SKU-C",
                        "price": "49.99",
                        "compareAtPrice": None,
                    },
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "49.99"},  # numeric
            {"variantId": "gid://shopify/ProductVariant/202", "price": "49.99"},  # GID
            {"variantId": "SKU-C", "price": "49.99"},  # SKU
        ],
        confirm=True,
    )
    assert out.startswith("CONFIRMED")
    assert len(fc.calls) == 2  # read + mutation only


# Mutation-error mapping -------------------------------------------------------


def test_user_errors_dotted_path_formatting():
    tools, fc = _build(
        [
            _read_response(),
            {
                "productVariantsBulkUpdate": {
                    "product": {"id": "gid://shopify/Product/100"},
                    "productVariants": [],
                    "userErrors": [
                        {
                            "field": ["variants", "0", "compareAtPrice"],
                            "message": "Compare at price must be higher than price",
                        }
                    ],
                }
            },
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "49.99", "compareAtPrice": "40.00"},
        ],
        confirm=True,
    )
    assert out.startswith(
        "Error: variants.0.compareAtPrice: Compare at price must be higher than price"
    )


def test_user_errors_missing_field_renders_no_field_token():
    tools, fc = _build(
        [
            _read_response(),
            {
                "productVariantsBulkUpdate": {
                    "product": {"id": "gid://shopify/Product/100"},
                    "productVariants": [],
                    "userErrors": [{"field": None, "message": "Generic failure"}],
                }
            },
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99"}],
        confirm=True,
    )
    assert out.startswith("Error: (no field): Generic failure")


# Caller-supplied numeric/GID not on product ---------------------------------


def test_unknown_numeric_id_shown_in_preview():
    # Numeric/GID short-circuits the in-memory resolver without consulting the
    # variants list — so the by_gid lookup is what catches it.
    tools, fc = _build(
        [
            _read_response(
                [
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "SKU-B",
                        "price": "39.99",
                        "compareAtPrice": None,
                    }
                ]
            )
        ]
    )
    out = tools["update_product_pricing"]("100", variants=[{"variantId": "201", "price": "49.99"}])
    assert "(variant not found on product)" in out


def test_unknown_numeric_id_rejected_at_confirm():
    tools, fc = _build(
        [
            _read_response(
                [
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "SKU-B",
                        "price": "39.99",
                        "compareAtPrice": None,
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99"}],
        confirm=True,
    )
    assert out.startswith("Error: resolved variant(s) not found on product:")
    assert "201" in out
    # No mutation call — bail before sending.
    assert len(fc.calls) == 1


# At-cap warning ---------------------------------------------------------------


def test_at_cap_warning_in_confirmed():
    variants = [
        {
            "id": f"gid://shopify/ProductVariant/{i}",
            "sku": f"SKU-{i}",
            "price": "10.00",
            "compareAtPrice": None,
        }
        for i in range(1, 251)
    ]
    tools, fc = _build(
        [
            {
                "product": {
                    "id": "gid://shopify/Product/100",
                    "title": "Big",
                    "variants": {"nodes": variants},
                }
            },
            _mutation_ok(
                [
                    {
                        "id": "gid://shopify/ProductVariant/1",
                        "sku": "SKU-1",
                        "price": "11.00",
                        "compareAtPrice": None,
                    }
                ]
            ),
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "1", "price": "11.00"}],
        confirm=True,
    )
    assert "WARNING: variant read hit the 250-variant page cap" in out


# JSON tail shape pins --------------------------------------------------------
#
# Spec amendment at shopify-aon-mcp-catalog-tools-spec.md:48 — every return is
# human-readable head + fenced ```json``` tail. These tests assert the tail's
# shape independently of head wording so the machine-readable contract stays
# pinned even if the human-readable text gets cosmetic tweaks.


def test_tail_present_on_every_return_path():
    """Smoke check: parser succeeds on validation-reject output."""
    tools, _ = _build([])
    out = tools["update_product_pricing"]("100", variants=[])
    tail = _parse_tail(out)
    assert tail == {
        "ok": False,
        "variants": [],
        "errors": [{"message": "variants must be a non-empty list"}],
    }


def test_tail_validation_error_carries_message_only():
    # Tool-side validation errors have no Shopify field path — just a message.
    tools, _ = _build([])
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "abc"}],
    )
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["variants"] == []
    assert len(tail["errors"]) == 1
    assert "is not a positive decimal" in tail["errors"][0]["message"]


def test_tail_product_not_found_marks_ok_false():
    tools, _ = _build([{"product": None}])
    out = tools["update_product_pricing"]("100", variants=[{"variantId": "1", "price": "1.00"}])
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["errors"][0]["message"] == "No product found with id 100."


def test_tail_resolver_error_pass_through():
    # Unknown SKU after the read → resolver error → tail mirrors the message.
    tools, _ = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100", variants=[{"variantId": "SKU-MISSING", "price": "9.99"}]
    )
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert "SKU-MISSING" in tail["errors"][0]["message"]


def test_tail_preview_shows_projected_state():
    tools, _ = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "49.99", "compareAtPrice": "65.00"},
        ],
    )
    tail = _parse_tail(out)
    assert tail == {
        "ok": True,
        "variants": [
            {
                "id": "gid://shopify/ProductVariant/201",
                "sku": "SKU-A",
                "price": "49.99",
                "compareAtPrice": "65.00",
            }
        ],
        "errors": [],
    }


def test_tail_preview_unchanged_field_preserves_existing_value():
    # Caller only supplies price; tail should carry the existing compareAtPrice.
    tools, _ = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99"}],
    )
    tail = _parse_tail(out)
    assert tail["variants"][0]["compareAtPrice"] == "50.00"


def test_tail_preview_clear_renders_as_null():
    tools, _ = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "compareAtPrice": None}],
    )
    tail = _parse_tail(out)
    assert tail["variants"][0]["compareAtPrice"] is None


def test_tail_preview_with_unknown_variant_marks_ok_false():
    # Numeric ID not on the product → preview is structurally a failure.
    tools, _ = _build(
        [
            _read_response(
                [
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "SKU-B",
                        "price": "39.99",
                        "compareAtPrice": None,
                    }
                ]
            )
        ]
    )
    out = tools["update_product_pricing"]("100", variants=[{"variantId": "201", "price": "9.99"}])
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["variants"] == []  # No resolvable variants to project.
    assert any("201" in e["message"] for e in tail["errors"])


def test_tail_confirmed_includes_shopify_returned_variants():
    tools, _ = _build(
        [
            _read_response(),
            _mutation_ok(
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
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99", "compareAtPrice": "65.00"}],
        confirm=True,
    )
    tail = _parse_tail(out)
    assert tail == {
        "ok": True,
        "variants": [
            {
                "id": "gid://shopify/ProductVariant/201",
                "sku": "SKU-A",
                "price": "49.99",
                "compareAtPrice": "65.00",
            }
        ],
        "errors": [],
    }


def test_tail_no_op_carries_projected_state():
    tools, _ = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "39.99", "compareAtPrice": "50.00"},
        ],
        confirm=True,
    )
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["errors"] == []
    assert tail["variants"][0]["id"] == "gid://shopify/ProductVariant/201"
    assert tail["variants"][0]["price"] == "39.99"


def test_tail_user_errors_passed_through_verbatim():
    # Shopify's userError objects (with `field` path) land in the tail unchanged.
    tools, _ = _build(
        [
            _read_response(),
            {
                "productVariantsBulkUpdate": {
                    "product": {"id": "gid://shopify/Product/100"},
                    "productVariants": [],
                    "userErrors": [
                        {
                            "field": ["variants", "0", "compareAtPrice"],
                            "message": "Compare at price must be higher than price",
                        }
                    ],
                }
            },
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99", "compareAtPrice": "40.00"}],
        confirm=True,
    )
    tail = _parse_tail(out)
    assert tail == {
        "ok": False,
        "variants": [],
        "errors": [
            {
                "field": ["variants", "0", "compareAtPrice"],
                "message": "Compare at price must be higher than price",
            }
        ],
    }


def test_tail_post_resolution_dup_marks_ok_false():
    tools, _ = _build([_read_response()])
    out = tools["update_product_pricing"](
        "100",
        variants=[
            {"variantId": "201", "price": "49.99"},
            {"variantId": "SKU-A", "compareAtPrice": "65.00"},
        ],
        confirm=True,
    )
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert "resolves to the same variant" in tail["errors"][0]["message"]


def test_tail_unknown_gids_at_confirm_marks_ok_false():
    tools, _ = _build(
        [
            _read_response(
                [
                    {
                        "id": "gid://shopify/ProductVariant/202",
                        "sku": "SKU-B",
                        "price": "39.99",
                        "compareAtPrice": None,
                    }
                ]
            )
        ]
    )
    out = tools["update_product_pricing"](
        "100",
        variants=[{"variantId": "201", "price": "49.99"}],
        confirm=True,
    )
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert any("201" in e["message"] for e in tail["errors"])


# ---------- Helpers — Story 9.1 (update_product_category) ----------


def _taxonomy_response(*nodes_kwargs):
    nodes = []
    for kw in nodes_kwargs:
        nodes.append(
            {
                "id": kw["id"],
                "fullName": kw.get("fullName", ""),
                "name": kw.get("name", ""),
                "level": kw.get("level", 3),
                "isLeaf": kw.get("isLeaf", True),
                "isRoot": kw.get("isRoot", False),
            }
        )
    return {"taxonomy": {"categories": {"nodes": nodes}}}


def _product_read_response(
    *,
    pid="5234567890",
    title="MCP Test Product — DO NOT PUBLISH",
    category_id=None,
    category_full_name=None,
    category_name=None,
):
    cat = (
        None
        if category_id is None
        else {
            "id": category_id,
            "fullName": category_full_name,
            "name": category_name,
        }
    )
    return {
        "product": {
            "id": f"gid://shopify/Product/{pid}",
            "title": title,
            "category": cat,
        }
    }


def _product_update_ok(
    *,
    pid="5234567890",
    title="MCP Test Product — DO NOT PUBLISH",
    category_id="gid://shopify/TaxonomyCategory/aa-1-13-9",
    category_full_name="Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
    category_name="Sweatshirts",
):
    return {
        "productUpdate": {
            "product": {
                "id": f"gid://shopify/Product/{pid}",
                "title": title,
                "category": {
                    "id": category_id,
                    "fullName": category_full_name,
                    "name": category_name,
                },
            },
            "userErrors": [],
        }
    }


def _product_update_err(field="category", message="invalid category for product"):
    return {
        "productUpdate": {
            "product": None,
            "userErrors": [{"field": field, "message": message}],
        }
    }


def _extract_json_tail(output: str) -> dict:
    """Pull the fenced ```json ...``` block out of a Story 9.1 tool's hybrid output.

    9.1 emits the JSON tail with `json.dumps(...)` (no indent), so the tail is a
    single line. 9.3 uses `json.dumps(..., indent=2)` via `_parse_tail`. Both
    helpers coexist — each test uses the one matching its tool's emitter.
    """
    marker = "```json\n"
    start = output.rindex(marker) + len(marker)
    end = output.rindex("\n```")
    return json.loads(output[start:end])


def test_update_product_category_numeric_id_resolved_to_gid_before_mutation():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="crewneck sweatshirt",
        confirm=True,
    )
    # The mutation variable's product.id MUST be a GID.
    _, vars_put = fc.calls[2]
    assert vars_put["product"]["id"] == "gid://shopify/Product/5234567890"
    assert fc.calls[2][0] == UPDATE_PRODUCT_CATEGORY
    body = _extract_json_tail(out)
    assert body["ok"] is True
    assert body["errors"] == []


def test_update_product_category_gid_passthrough_skips_handle_lookup():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    tools["update_product_category"](
        product_id="gid://shopify/Product/5234567890",
        category="sweatshirt",
        confirm=True,
    )
    # Three calls only: taxonomy search + product read + mutation. No handle lookup.
    assert len(fc.calls) == 3
    assert fc.calls[0][0] == TAXONOMY_SEARCH
    assert fc.calls[1][0] == GET_PRODUCT_CATEGORY


def test_update_product_category_handle_resolves_via_product_by_handle():
    tools, fc = _build(
        [
            # Handle lookup short-circuits with the product's GID.
            {
                "productByHandle": {
                    "id": "gid://shopify/Product/5234567890",
                    "title": "MCP Test Product — DO NOT PUBLISH",
                    "category": None,
                }
            },
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="mcp-test-product",
        category="sweatshirt",
        confirm=True,
    )
    assert fc.calls[0][0] == GET_PRODUCT_BY_HANDLE_MIN
    assert fc.calls[0][1] == {"handle": "mcp-test-product"}
    assert fc.calls[-1][1]["product"]["id"] == "gid://shopify/Product/5234567890"
    body = _extract_json_tail(out)
    assert body["ok"] is True


def test_update_product_category_handle_not_found_errors_cleanly():
    tools, fc = _build([{"productByHandle": None}])
    out = tools["update_product_category"](
        product_id="ghost-handle",
        category="sweatshirt",
    )
    assert "No product found with handle" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    # No taxonomy / mutation calls — bailed at handle resolution.
    assert len(fc.calls) == 1


def test_update_product_category_empty_product_id_errors_without_network():
    tools, fc = _build([])
    out = tools["update_product_category"](product_id="", category="sweatshirt")
    assert "product_id must be a non-empty string" in out
    assert fc.calls == []
    body = _extract_json_tail(out)
    assert body["ok"] is False


def test_update_product_category_malformed_product_gid_errors():
    tools, fc = _build([])
    out = tools["update_product_category"](
        product_id="gid://shopify/Product/",
        category="sweatshirt",
    )
    assert "Empty product GID body" in out
    assert fc.calls == []


# ---------- AC #2 — category accepts GID or search ----------


def test_update_product_category_with_category_gid_skips_taxonomy_lookup():
    tools, fc = _build(
        [
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="gid://shopify/TaxonomyCategory/aa-1-13-9",
        confirm=True,
    )
    # Only 2 calls: GET_PRODUCT_CATEGORY + UPDATE. No TAXONOMY_SEARCH.
    assert len(fc.calls) == 2
    assert TAXONOMY_SEARCH not in [c[0] for c in fc.calls]
    assert fc.calls[0][0] == GET_PRODUCT_CATEGORY
    assert fc.calls[1][0] == UPDATE_PRODUCT_CATEGORY
    body = _extract_json_tail(out)
    assert body["ok"] is True


def test_update_product_category_malformed_category_gid_errors():
    tools, fc = _build([])
    out = tools["update_product_category"](
        product_id="5234567890",
        category="gid://shopify/TaxonomyCategory/",
    )
    assert "Empty TaxonomyCategory GID body" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    assert fc.calls == []


def test_update_product_category_empty_category_errors():
    tools, fc = _build([])
    out = tools["update_product_category"](product_id="5234567890", category="   ")
    assert "category must be a non-empty string" in out
    assert fc.calls == []


# ---------- AC #3 — reject-ambiguous ----------


def test_update_product_category_reject_ambiguous_fails_on_multiple_leaves():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-10",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > T-Shirts",
                    "name": "T-Shirts",
                },
            )
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="shirt",
        resolve_strategy="reject-ambiguous",
        confirm=True,
    )
    assert "reject-ambiguous" in out
    # Story 9.8 AC6: error wording must say "taxonomy categories", not
    # "leaf categories". Locks in the rewording on the reject-ambiguous path.
    assert "2 taxonomy categories matched" in out
    assert "leaf" not in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    # No GET_PRODUCT_CATEGORY, no UPDATE_PRODUCT_CATEGORY — bailed at search.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == TAXONOMY_SEARCH


def test_update_product_category_reject_ambiguous_succeeds_on_single_leaf():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirt",
        resolve_strategy="reject-ambiguous",
        confirm=True,
    )
    body = _extract_json_tail(out)
    assert body["ok"] is True
    assert body["alternates"] == []


# ---------- AC #4 — best-match picks first, lists alternates ----------


def test_update_product_category_best_match_picks_first_leaf_and_lists_alternates():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-10",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > T-Shirts",
                    "name": "T-Shirts",
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-11",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Polo Shirts",
                    "name": "Polo Shirts",
                },
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="shirt",
        resolve_strategy="best-match",
        confirm=True,
    )
    # Picked the first leaf.
    _, vars_put = fc.calls[2]
    assert vars_put["product"]["category"] == "gid://shopify/TaxonomyCategory/aa-1-13-9"
    body = _extract_json_tail(out)
    assert body["ok"] is True
    assert len(body["alternates"]) == 2
    assert body["alternates"][0]["id"] == "gid://shopify/TaxonomyCategory/aa-1-13-10"
    assert body["alternates"][1]["id"] == "gid://shopify/TaxonomyCategory/aa-1-13-11"
    # No `score` field — Shopify doesn't return one, order alone carries the signal.
    assert "score" not in body["alternates"][0]
    assert "score" not in body["alternates"][1]


def test_update_product_category_best_match_picks_leaf_via_prefix_over_unrelated_parents():
    # Story 9.8 dropped the isLeaf filter — parent and intermediate nodes now
    # compete with leaves. For input "sweatshirt", Tier 2 (casefold prefix)
    # picks "Sweatshirts" because neither the root "Apparel & Accessories" nor
    # the intermediate "Shirts & Tops" starts with "sweatshirt". This is the
    # regression guard for the leaf-wins-when-parent-doesn't-prefix-match path.
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa",
                    "fullName": "Apparel & Accessories",
                    "name": "Apparel & Accessories",
                    "isLeaf": False,
                    "isRoot": True,
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops",
                    "name": "Shirts & Tops",
                    "isLeaf": False,
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                    "isLeaf": True,
                },
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirt",
        confirm=True,
    )
    _, vars_put = fc.calls[2]
    # "Sweatshirts" wins via Tier 2 prefix; root + intermediate don't prefix-match.
    assert vars_put["product"]["category"] == "gid://shopify/TaxonomyCategory/aa-1-13-9"


# ---------- Story 9.8 — non-leaf taxonomy node matching ----------


def test_update_product_category_exact_resolves_parent_node_apparel_accessories():
    # Story 9.8 AC1: input "Apparel & Accessories" with resolve_strategy='exact'
    # must resolve to the parent node `aa`, not error out. Before 9.8 the
    # resolver filtered to leaves and failed with "no leaf category matched".
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa",
                    "fullName": "Apparel & Accessories",
                    "name": "Apparel & Accessories",
                    "isLeaf": False,
                    "isRoot": True,
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                    "isLeaf": True,
                },
            ),
            _product_read_response(),
            _product_update_ok(
                category_id="gid://shopify/TaxonomyCategory/aa",
                category_full_name="Apparel & Accessories",
                category_name="Apparel & Accessories",
            ),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="Apparel & Accessories",
        resolve_strategy="exact",
        confirm=True,
    )
    _, vars_put = fc.calls[2]
    assert vars_put["product"]["category"] == "gid://shopify/TaxonomyCategory/aa"
    body = _extract_json_tail(out)
    assert body["ok"] is True


def test_update_product_category_exact_resolves_intermediate_clothing():
    # Story 9.8 AC2: input "Clothing" with resolve_strategy='exact' must
    # resolve to the intermediate node under `aa`. Same principle as AC1 but
    # for a mid-tree non-leaf.
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa",
                    "fullName": "Apparel & Accessories",
                    "name": "Apparel & Accessories",
                    "isLeaf": False,
                    "isRoot": True,
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1",
                    "fullName": "Apparel & Accessories > Clothing",
                    "name": "Clothing",
                    "isLeaf": False,
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                    "isLeaf": True,
                },
            ),
            _product_read_response(),
            _product_update_ok(
                category_id="gid://shopify/TaxonomyCategory/aa-1",
                category_full_name="Apparel & Accessories > Clothing",
                category_name="Clothing",
            ),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="Clothing",
        resolve_strategy="exact",
        confirm=True,
    )
    _, vars_put = fc.calls[2]
    assert vars_put["product"]["category"] == "gid://shopify/TaxonomyCategory/aa-1"
    body = _extract_json_tail(out)
    assert body["ok"] is True


def test_update_product_category_best_match_prefers_parent_full_string_over_token_leaf():
    # Story 9.8 AC3: input "Apparel & Accessories" with best-match must NOT
    # land on Pager Accessories (or any unrelated *_Accessories leaf). Tier 1
    # full-string match on the parent `aa` beats Tier 3 fallback. Fixture
    # mirrors the actual 2026-05-12 smoke-test response.
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa",
                    "fullName": "Apparel & Accessories",
                    "name": "Apparel & Accessories",
                    "isLeaf": False,
                    "isRoot": True,
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/el-4-12",
                    "fullName": "Electronics > Communications > Pager Accessories",
                    "name": "Pager Accessories",
                    "isLeaf": True,
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/hg-1-1",
                    "fullName": "Hardware > Tools > Auger Accessories",
                    "name": "Auger Accessories",
                    "isLeaf": True,
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/hg-1-2",
                    "fullName": "Hardware > Building Materials > Cable Tray Accessories",
                    "name": "Cable Tray Accessories",
                    "isLeaf": True,
                },
            ),
            _product_read_response(),
            _product_update_ok(
                category_id="gid://shopify/TaxonomyCategory/aa",
                category_full_name="Apparel & Accessories",
                category_name="Apparel & Accessories",
            ),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="Apparel & Accessories",
        resolve_strategy="best-match",
        confirm=True,
    )
    _, vars_put = fc.calls[2]
    assert vars_put["product"]["category"] == "gid://shopify/TaxonomyCategory/aa"
    body = _extract_json_tail(out)
    assert body["ok"] is True
    # The three unrelated *_Accessories leaves drop into alternates, in
    # Shopify's relevance order.
    assert len(body["alternates"]) == 3
    assert body["alternates"][0]["id"] == "gid://shopify/TaxonomyCategory/el-4-12"
    assert body["alternates"][1]["id"] == "gid://shopify/TaxonomyCategory/hg-1-1"
    assert body["alternates"][2]["id"] == "gid://shopify/TaxonomyCategory/hg-1-2"


def test_update_product_category_best_match_tier1_multi_hit_falls_through():
    # Coverage: when two candidates both casefold-equal the needle, Tier 1
    # must NOT silently pick one — it falls through. Tier 2 ALSO has 2 hits
    # in this fixture (both dup-1 and dup-2 prefix the needle "hat"), so the
    # scorer falls all the way to Tier 3 (Shopify's relevance order). The
    # test name omits a destination tier because the depth of the fall
    # depends on what Tier 2 produces.
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/dup-1",
                    "fullName": "Hat",
                    "name": "Hat",
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/dup-2",
                    "fullName": "HAT",
                    "name": "HAT",
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/uniq-3",
                    "fullName": "Apparel & Accessories > Hats > Wide-Brim Hat",
                    "name": "Wide-Brim Hat",
                },
            ),
            _product_read_response(),
            _product_update_ok(
                category_id="gid://shopify/TaxonomyCategory/uniq-3",
                category_full_name="Apparel & Accessories > Hats > Wide-Brim Hat",
                category_name="Wide-Brim Hat",
            ),
        ]
    )
    # Needle is "hat" — Tier 1 matches dup-1 AND dup-2 (both casefold-equal
    # "hat"), so it falls through. Tier 2 only matches dup-1 and dup-2 (both
    # prefix "hat"); Wide-Brim Hat does NOT prefix-match. Tier 2 has 2 → falls
    # through. Tier 3 picks the first candidate (dup-1).
    out = tools["update_product_category"](
        product_id="5234567890",
        category="hat",
        resolve_strategy="best-match",
        confirm=True,
    )
    _, vars_put = fc.calls[2]
    # Tier 3 fallback — first candidate in Shopify's relevance order.
    assert vars_put["product"]["category"] == "gid://shopify/TaxonomyCategory/dup-1"
    body = _extract_json_tail(out)
    assert body["ok"] is True


def test_update_product_category_best_match_no_tier_hit_falls_back_to_shopify_rank():
    # Coverage for Tier 3: no candidate's name/fullName equals or prefixes
    # the input, so best-match returns candidates[0] — Shopify's top-ranked
    # result. This is the existing pre-9.8 behavior, preserved.
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-10",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > T-Shirts",
                    "name": "T-Shirts",
                },
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    # Needle is "crewneck" — no candidate name/fullName equals or starts with
    # "crewneck", so Tier 3 picks the first leaf (Sweatshirts).
    out = tools["update_product_category"](
        product_id="5234567890",
        category="crewneck",
        resolve_strategy="best-match",
        confirm=True,
    )
    _, vars_put = fc.calls[2]
    assert vars_put["product"]["category"] == "gid://shopify/TaxonomyCategory/aa-1-13-9"
    body = _extract_json_tail(out)
    assert body["ok"] is True
    # T-Shirts drops to alternates.
    assert len(body["alternates"]) == 1
    assert body["alternates"][0]["id"] == "gid://shopify/TaxonomyCategory/aa-1-13-10"


# ---------- AC #5 — exact ----------


def test_update_product_category_exact_succeeds_on_full_name_match():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
        resolve_strategy="exact",
        confirm=True,
    )
    body = _extract_json_tail(out)
    assert body["ok"] is True


def test_update_product_category_exact_succeeds_on_name_match():
    # Match by short `name` too — the exact branch checks both fullName and name.
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirts",
        resolve_strategy="exact",
        confirm=True,
    )
    body = _extract_json_tail(out)
    assert body["ok"] is True


def test_update_product_category_exact_fails_when_no_exact_match():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                }
            )
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="crewneck pullover",
        resolve_strategy="exact",
        confirm=True,
    )
    assert "exact" in out
    # Story 9.8 AC6: the error message must say "taxonomy category", not
    # "leaf category". Locks in the rewording.
    assert "no taxonomy category matched" in out
    assert "leaf" not in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    # Mutation must NOT have run.
    assert len(fc.calls) == 1


def test_update_product_category_exact_fails_on_multiple_exact_matches():
    # Defensive: if two leaves happen to share the same fullName casefold,
    # exact must NOT silently pick one.
    tools, _fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-99-9",
                    "fullName": "SWEATSHIRTS",
                    "name": "Sweatshirts",
                },
            )
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirts",
        resolve_strategy="exact",
        confirm=True,
    )
    body = _extract_json_tail(out)
    assert body["ok"] is False
    assert "exact" in out
    # Story 9.8 AC6: error wording must say "taxonomy categories", not
    # "leaf categories". Locks in the rewording on the >1 match path.
    assert "2 taxonomy categories matched" in out
    assert "leaf" not in out


# ---------- AC #6 — productUpdate input shape ----------


def test_update_product_category_mutation_uses_product_update_input_shape():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirts",
        confirm=True,
    )
    query, variables = fc.calls[2]
    assert query == UPDATE_PRODUCT_CATEGORY
    # The spec pins `product: ProductUpdateInput!` — NOT `input: ProductInput!`.
    assert "product" in variables
    assert variables["product"] == {
        "id": "gid://shopify/Product/5234567890",
        "category": "gid://shopify/TaxonomyCategory/aa-1-13-9",
    }


# ---------- AC #7 — dry-run (confirm=False) ----------


def test_update_product_category_dry_run_no_mutation():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirt",
        confirm=False,
    )
    assert out.startswith("PREVIEW —"), out
    # NO UPDATE_PRODUCT_CATEGORY call in the recorded queries.
    queries = [c[0] for c in fc.calls]
    assert UPDATE_PRODUCT_CATEGORY not in queries
    body = _extract_json_tail(out)
    assert body["preview"] is True
    assert body["ok"] is True
    # The preview product carries the resolved target category so callers can
    # inspect exactly what *would* be written.
    assert body["product"]["category"]["id"] == "gid://shopify/TaxonomyCategory/aa-1-13-9"
    assert "Reply with confirm=True" in out


def test_update_product_category_dry_run_with_category_gid_still_skips_mutation():
    tools, fc = _build([_product_read_response()])
    out = tools["update_product_category"](
        product_id="5234567890",
        category="gid://shopify/TaxonomyCategory/aa-1-13-9",
        confirm=False,
    )
    assert out.startswith("PREVIEW —")
    assert UPDATE_PRODUCT_CATEGORY not in [c[0] for c in fc.calls]
    body = _extract_json_tail(out)
    assert body["preview"] is True


# ---------- AC #8 — userErrors verbatim + transport errors ----------


def test_update_product_category_user_errors_surface_in_output():
    tools, _fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_err(field="category", message="invalid category for product"),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirts",
        confirm=True,
    )
    assert "invalid category for product" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    # Verbatim userErrors land in the JSON tail.
    assert body["errors"] == [{"field": "category", "message": "invalid category for product"}]


def test_update_product_category_transport_error_on_taxonomy_search_surfaces_structured_error():
    # AC #8 — non-200 on the taxonomy lookup must NOT propagate uncaught.
    tools, fc = _build([RuntimeError("Shopify HTTP error: 502 Bad Gateway")])
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirt",
        confirm=True,
    )
    # Header annotates exception type so UX disambiguates transport vs logic failures.
    assert "Taxonomy search failed (RuntimeError)" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    assert body["errors"][0]["stage"] == "category-resolve"
    assert "502" in body["errors"][0]["message"]
    # Product read + mutation must NOT have been attempted.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == TAXONOMY_SEARCH


def test_update_product_category_transport_error_on_handle_lookup_surfaces_structured_error():
    # AC #8 — non-200 on the productByHandle lookup must NOT propagate uncaught.
    tools, fc = _build([RuntimeError("Shopify HTTP error: 503 Service Unavailable")])
    out = tools["update_product_category"](
        product_id="mcp-test-product",
        category="sweatshirt",
        confirm=True,
    )
    assert "Handle lookup failed (RuntimeError)" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    assert body["errors"][0]["stage"] == "product-resolve"
    assert "503" in body["errors"][0]["message"]
    # Taxonomy + product read + mutation must NOT have been attempted.
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_BY_HANDLE_MIN


def test_update_product_category_transport_error_on_read_surfaces_structured_error():
    tools, fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            RuntimeError("Shopify HTTP error: 503 Service Unavailable"),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirts",
        confirm=True,
    )
    assert "Failed to read current product" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    assert body["errors"][0]["stage"] == "product-read"
    assert "503" in body["errors"][0]["message"]
    # Mutation was never attempted (responses had only 2 entries; no AssertionError).
    assert len(fc.calls) == 2


def test_update_product_category_transport_error_on_mutation_surfaces_structured_error():
    tools, _fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            RuntimeError("Shopify HTTP error: 500"),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirts",
        confirm=True,
    )
    assert "Mutation failed" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    assert body["errors"][0]["stage"] == "product-update"


# ---------- AC #9 — idempotent no-op ----------


def test_update_product_category_idempotent_when_already_set():
    # The product already has the target category — no mutation.
    target_gid = "gid://shopify/TaxonomyCategory/aa-1-13-9"
    target_full = "Apparel & Accessories > Clothing > Shirts & Tops > Sweatshirts"
    tools, fc = _build(
        [
            _taxonomy_response({"id": target_gid, "fullName": target_full, "name": "Sweatshirts"}),
            _product_read_response(
                category_id=target_gid,
                category_full_name=target_full,
                category_name="Sweatshirts",
            ),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirt",
        confirm=True,
    )
    assert "no-op, already set" in out
    body = _extract_json_tail(out)
    assert body["ok"] is True
    assert body["errors"] == []
    assert UPDATE_PRODUCT_CATEGORY not in [c[0] for c in fc.calls]


# ---------- AC #10 — return shape ----------


def test_update_product_category_returns_fenced_json_tail_block():
    tools, _fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirts",
        confirm=True,
    )
    assert "```json" in out
    assert out.rstrip().endswith("```")
    body = _extract_json_tail(out)
    # Spec's return shape: ok, product { id, title, category }, alternates, errors.
    assert set(body.keys()) >= {"ok", "product", "alternates", "errors", "preview"}
    assert set(body["product"].keys()) >= {"id", "title", "category"}
    assert set(body["product"]["category"].keys()) >= {"id", "fullName"}


# ---------- Edge cases & validation ----------


def test_update_product_category_zero_search_results_errors_cleanly():
    tools, fc = _build([_taxonomy_response()])
    out = tools["update_product_category"](
        product_id="5234567890",
        category="unicorn dust",
    )
    assert "unicorn dust" in out
    assert "No taxonomy categories matched" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    # Only the taxonomy search ran.
    assert len(fc.calls) == 1


def test_update_product_category_invalid_resolve_strategy_errors_before_network():
    tools, fc = _build([])
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirt",
        resolve_strategy="fuzzy-magic",
    )
    assert "Invalid resolve_strategy" in out
    assert "fuzzy-magic" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
    assert fc.calls == []


def test_update_product_category_preview_shows_alternates_count_in_human_text():
    # Two leaves → header should mention the runner-up count.
    tools, _fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                },
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-10",
                    "fullName": "T-Shirts",
                    "name": "T-Shirts",
                },
            ),
            _product_read_response(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="shirt",
        confirm=False,
    )
    assert "runner-up" in out


def test_update_product_category_preview_old_block_shows_existing_category():
    tools, _fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(
                category_id="gid://shopify/TaxonomyCategory/aa-1-13-10",
                category_full_name="T-Shirts",
                category_name="T-Shirts",
            ),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirt",
        confirm=False,
    )
    assert "T-Shirts" in out
    assert "gid://shopify/TaxonomyCategory/aa-1-13-10" in out


def test_update_product_category_done_branch_strips_preview_marker():
    tools, _fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirts",
        confirm=True,
    )
    assert out.startswith("Done. Update product category"), out
    assert "PREVIEW" not in out
    assert "Reply with confirm=True" not in out
    body = _extract_json_tail(out)
    assert body["preview"] is False
    assert body["ok"] is True


def test_update_product_category_with_gid_no_existing_category_writes_cleanly():
    # Category GID passthrough on a product with no existing category — covers
    # the `old_block == '(none)'` branch on the execute path.
    tools, _fc = _build(
        [
            _product_read_response(category_id=None),
            _product_update_ok(),
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="gid://shopify/TaxonomyCategory/aa-1-13-9",
        confirm=True,
    )
    assert "(none)" in out
    body = _extract_json_tail(out)
    assert body["ok"] is True


def test_update_product_category_post_update_product_node_null_yields_none_snapshot():
    # productUpdate returns no product node (rare but possible Shopify response).
    tools, _fc = _build(
        [
            _taxonomy_response(
                {
                    "id": "gid://shopify/TaxonomyCategory/aa-1-13-9",
                    "fullName": "Sweatshirts",
                    "name": "Sweatshirts",
                }
            ),
            _product_read_response(),
            {"productUpdate": {"product": None, "userErrors": []}},
        ]
    )
    out = tools["update_product_category"](
        product_id="5234567890",
        category="sweatshirts",
        confirm=True,
    )
    body = _extract_json_tail(out)
    assert body["ok"] is True
    assert body["product"] is None


# ---------- Helpers — Story 9.2 (update_product_vendor) ----------


def _vendor_read(pid="123", vendor="Nike", title="Tee"):
    return {
        "product": {
            "id": f"gid://shopify/Product/{pid}",
            "title": title,
            "vendor": vendor,
        }
    }


def _vendor_read_by_handle(pid="123", vendor="Nike", title="Tee"):
    return {
        "productByHandle": {
            "id": f"gid://shopify/Product/{pid}",
            "title": title,
            "vendor": vendor,
        }
    }


def _update_ok(pid="123", vendor="Vanish"):
    return {
        "productUpdate": {
            "product": {"id": f"gid://shopify/Product/{pid}", "vendor": vendor},
            "userErrors": [],
        }
    }


def _update_user_err(field, message):
    return {
        "productUpdate": {
            "product": None,
            "userErrors": [{"field": field, "message": message}],
        }
    }


def _extract_json(output):
    """Pull the fenced ```json ...``` block out of a tool return value."""
    head = output.index("```json\n") + len("```json\n")
    tail = output.index("\n```", head)
    return json.loads(output[head:tail])


# ---------- productId resolution ----------


def test_update_product_vendor_numeric_id_resolved():
    tools, fc = _build(
        [
            _vendor_read(pid="5234567890", vendor="Nike"),
            _update_ok(pid="5234567890", vendor="Vanish"),
        ]
    )
    out = tools["update_product_vendor"](product_id="5234567890", vendor="Vanish", confirm=True)
    assert fc.calls[0][0] == GET_PRODUCT_VENDOR
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/5234567890"}
    assert fc.calls[1][0] == UPDATE_PRODUCT_VENDOR
    assert fc.calls[1][1] == {
        "product": {"id": "gid://shopify/Product/5234567890", "vendor": "Vanish"}
    }
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["preview"] is False
    assert payload["product"]["id"] == "gid://shopify/Product/5234567890"
    assert payload["product"]["vendor"] == "Vanish"
    assert payload["errors"] == []
    assert out.startswith("Done.")


def test_update_product_vendor_gid_passthrough():
    gid = "gid://shopify/Product/7777"
    tools, fc = _build([_vendor_read(pid="7777", vendor="Old"), _update_ok(pid="7777")])
    out = tools["update_product_vendor"](product_id=gid, vendor="Vanish", confirm=True)
    assert fc.calls[0][1] == {"id": gid}
    assert fc.calls[1][1]["product"]["id"] == gid
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["product"]["id"] == gid


def test_update_product_vendor_handle_resolved():
    tools, fc = _build([_vendor_read_by_handle(pid="42", vendor="Old"), _update_ok(pid="42")])
    out = tools["update_product_vendor"](product_id="cool-tee", vendor="Vanish", confirm=True)
    assert fc.calls[0][0] == GET_PRODUCT_VENDOR_BY_HANDLE
    assert fc.calls[0][1] == {"handle": "cool-tee"}
    assert fc.calls[1][1]["product"]["id"] == "gid://shopify/Product/42"
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["product"]["id"] == "gid://shopify/Product/42"


def test_update_product_vendor_handle_not_found():
    # productByHandle returns null when there is no matching product. The tool
    # must surface this as a structured error without ever calling productUpdate.
    tools, fc = _build([{"productByHandle": None}])
    out = tools["update_product_vendor"](product_id="nope-handle", vendor="Vanish", confirm=True)
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_VENDOR_BY_HANDLE
    assert "no product found" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"]
    assert payload["preview"] is False


def test_update_product_vendor_handle_not_found_dry_run():
    # Symmetric to ..._handle_not_found but with confirm=False. Pins that the
    # not-found error path emits preview=False regardless of confirm.
    tools, fc = _build([{"productByHandle": None}])
    out = tools["update_product_vendor"](product_id="nope-handle", vendor="Vanish", confirm=False)
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_VENDOR_BY_HANDLE
    assert "no product found" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["preview"] is False
    assert payload["errors"][0]["stage"] == "product-resolve"
    assert UPDATE_PRODUCT_VENDOR not in [c[0] for c in fc.calls]


def test_update_product_vendor_numeric_id_not_found():
    # GET_PRODUCT_VENDOR returns null when the numeric/GID id doesn't exist.
    tools, fc = _build([{"product": None}])
    out = tools["update_product_vendor"](product_id="999999", vendor="Vanish", confirm=False)
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_VENDOR
    payload = _extract_json(out)
    assert payload["ok"] is False
    # preview=False on every error path — "preview" means "this is what would
    # happen if you confirmed"; on a not-found error nothing would happen.
    assert payload["preview"] is False
    assert payload["errors"][0]["stage"] == "product-resolve"


def test_update_product_vendor_resolve_exception_surfaces_as_error():
    tools, fc = _build([RuntimeError("transport boom")])
    out = tools["update_product_vendor"](product_id="123", vendor="Vanish", confirm=True)
    assert len(fc.calls) == 1
    # Header annotates exception type so transport vs logic failures disambiguate.
    assert "Error resolving product_id (RuntimeError)" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"] == [{"message": "transport boom", "stage": "product-resolve"}]


def test_update_product_vendor_empty_product_id_fast_fails_without_network():
    # Empty product_id must short-circuit at the resolver guard before any
    # Shopify call (matches Story 9.1's `_resolve_product_gid` behavior).
    tools, fc = _build([])
    out = tools["update_product_vendor"](product_id="", vendor="Vanish", confirm=True)
    assert fc.calls == []
    assert "product_id must be a non-empty string" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "product-resolve"


def test_update_product_vendor_empty_gid_body_fast_fails_without_network():
    # `gid://shopify/Product/` (empty body after prefix) is malformed — error
    # before any Shopify call rather than letting the read return null.
    tools, fc = _build([])
    out = tools["update_product_vendor"](
        product_id="gid://shopify/Product/",
        vendor="Vanish",
        confirm=True,
    )
    assert fc.calls == []
    assert "Empty product GID body" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "product-resolve"


# ---------- vendor validation ----------


def test_update_product_vendor_rejects_empty_string():
    tools, fc = _build([])
    out = tools["update_product_vendor"](product_id="123", vendor="", confirm=True)
    assert fc.calls == [], "empty vendor must short-circuit before any Shopify call"
    assert "Error:" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"][0]["field"] == "vendor"
    assert payload["errors"][0]["stage"] == "validation"


def test_update_product_vendor_rejects_whitespace_only():
    tools, fc = _build([])
    out = tools["update_product_vendor"](product_id="123", vendor="   ", confirm=True)
    assert fc.calls == []
    assert "Error:" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["preview"] is False


def test_update_product_vendor_rejects_over_255_chars():
    tools, fc = _build([])
    long_vendor = "a" * (VENDOR_MAX_LEN + 1)
    out = tools["update_product_vendor"](product_id="123", vendor=long_vendor, confirm=True)
    assert fc.calls == [], ">255-char vendor must reject before any Shopify call"
    assert f"{VENDOR_MAX_LEN}" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["preview"] is False


def test_update_product_vendor_accepts_exact_255_chars():
    vendor = "a" * VENDOR_MAX_LEN
    tools, fc = _build([_vendor_read(pid="55", vendor="Old"), _update_ok(pid="55", vendor=vendor)])
    out = tools["update_product_vendor"](product_id="55", vendor=vendor, confirm=True)
    assert fc.calls[1][0] == UPDATE_PRODUCT_VENDOR
    assert fc.calls[1][1]["product"]["vendor"] == vendor
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["product"]["vendor"] == vendor


def test_update_product_vendor_trims_whitespace():
    tools, fc = _build(
        [_vendor_read(pid="33", vendor="Old"), _update_ok(pid="33", vendor="Vanish")]
    )
    out = tools["update_product_vendor"](product_id="33", vendor="  Vanish  ", confirm=True)
    sent = fc.calls[1][1]["product"]["vendor"]
    assert sent == "Vanish", f"expected trimmed 'Vanish', got {sent!r}"
    payload = _extract_json(out)
    assert payload["product"]["vendor"] == "Vanish"


# ---------- clearing semantics ----------


def test_update_product_vendor_clear_with_none():
    # vendor=None ⇒ Shopify gets vendor: null AND text shows "(cleared)".
    tools, fc = _build(
        [
            _vendor_read(pid="88", vendor="Nike"),
            {
                "productUpdate": {
                    "product": {"id": "gid://shopify/Product/88", "vendor": None},
                    "userErrors": [],
                }
            },
        ]
    )
    out = tools["update_product_vendor"](product_id="88", vendor=None, confirm=True)
    assert fc.calls[1][1]["product"]["vendor"] is None
    assert "(cleared)" in out
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["product"]["vendor"] is None


# ---------- dry-run / preview ----------


def test_update_product_vendor_dry_run_no_mutation():
    tools, fc = _build([_vendor_read(pid="123", vendor="Nike")])
    out = tools["update_product_vendor"](product_id="123", vendor="Vanish", confirm=False)
    assert out.startswith("PREVIEW —"), out
    assert "Reply with confirm=True" in out
    assert len(fc.calls) == 1, "preview must not issue UPDATE_PRODUCT_VENDOR"
    assert fc.calls[0][0] == GET_PRODUCT_VENDOR
    payload = _extract_json(out)
    assert payload["preview"] is True
    assert payload["ok"] is True
    assert payload["product"]["vendor"] == "Vanish"


# ---------- idempotency ----------


def test_update_product_vendor_idempotent_when_already_set():
    # Current vendor already equals target → no mutation call, ok=True.
    tools, fc = _build([_vendor_read(pid="123", vendor="Vanish")])
    out = tools["update_product_vendor"](product_id="123", vendor="Vanish", confirm=True)
    assert len(fc.calls) == 1, "idempotent path must skip the mutation"
    assert "no-op" in out
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["preview"] is False
    assert payload["product"]["vendor"] == "Vanish"


def test_update_product_vendor_idempotent_when_both_empty():
    # Current vendor is null, target is null (clear-when-already-clear).
    tools, fc = _build([_vendor_read(pid="123", vendor=None)])
    out = tools["update_product_vendor"](product_id="123", vendor=None, confirm=True)
    assert len(fc.calls) == 1
    assert "no-op" in out
    assert "(cleared)" in out
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["preview"] is False
    assert payload["product"]["vendor"] is None


def test_update_product_vendor_idempotent_when_current_whitespace_target_none():
    # Current vendor is whitespace-only, target is null — both normalize to None.
    tools, fc = _build([_vendor_read(pid="123", vendor="   ")])
    out = tools["update_product_vendor"](product_id="123", vendor=None, confirm=True)
    assert len(fc.calls) == 1
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["preview"] is False


# ---------- error surfacing ----------


def test_update_product_vendor_user_errors_surface_in_output():
    tools, fc = _build(
        [
            _vendor_read(pid="123", vendor="Old"),
            _update_user_err("vendor", "Vendor is too long"),
        ]
    )
    out = tools["update_product_vendor"](product_id="123", vendor="Vanish", confirm=True)
    assert fc.calls[1][0] == UPDATE_PRODUCT_VENDOR
    assert "Vendor is too long" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"] == [{"field": "vendor", "message": "Vendor is too long"}]
    assert payload["preview"] is False


def test_update_product_vendor_mutation_exception_surfaced():
    tools, fc = _build([_vendor_read(pid="123", vendor="Old"), RuntimeError("HTTP 503")])
    out = tools["update_product_vendor"](product_id="123", vendor="Vanish", confirm=True)
    assert "Error calling productUpdate (RuntimeError)" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"] == [{"message": "HTTP 503", "stage": "product-update"}]


# ---------- JSON tail contract ----------


def test_update_product_vendor_returns_json_tail_block():
    tools, _fc = _build(
        [_vendor_read(pid="123", vendor="Old"), _update_ok(pid="123", vendor="Vanish")]
    )
    out = tools["update_product_vendor"](product_id="123", vendor="Vanish", confirm=True)
    assert "```json\n" in out
    payload = _extract_json(out)
    # All four spec-required keys, none missing.
    assert set(payload.keys()) == {"ok", "product", "errors", "preview"}
    assert set(payload["product"].keys()) == {"id", "vendor"}


def test_update_product_vendor_post_mutation_vendor_echoed():
    # If Shopify normalizes the vendor (e.g. trims further), the JSON tail
    # MUST reflect what Shopify stored, not what we sent.
    tools, _fc = _build(
        [
            _vendor_read(pid="123", vendor="Old"),
            _update_ok(pid="123", vendor="VanishNormalized"),
        ]
    )
    out = tools["update_product_vendor"](product_id="123", vendor="Vanish", confirm=True)
    payload = _extract_json(out)
    assert payload["product"]["vendor"] == "VanishNormalized"


def test_update_product_vendor_post_mutation_missing_vendor_field_falls_back():
    # Defensive: if Shopify's response omits `vendor`, the tail keeps the
    # value we sent so the JSON shape stays consistent.
    tools, _fc = _build(
        [
            _vendor_read(pid="123", vendor="Old"),
            {
                "productUpdate": {
                    "product": {"id": "gid://shopify/Product/123"},
                    "userErrors": [],
                }
            },
        ]
    )
    out = tools["update_product_vendor"](product_id="123", vendor="Vanish", confirm=True)
    payload = _extract_json(out)
    assert payload["product"]["vendor"] == "Vanish"


# ---------- _vendor_text unit tests (Suggestion #1) ----------


def test_vendor_text_none_returns_cleared():
    from tools.catalog_hygiene import _vendor_text

    assert _vendor_text(None) == "(cleared)"


def test_vendor_text_empty_string_returns_cleared():
    from tools.catalog_hygiene import _vendor_text

    assert _vendor_text("") == "(cleared)"


def test_vendor_text_whitespace_only_returns_cleared():
    from tools.catalog_hygiene import _vendor_text

    assert _vendor_text("   ") == "(cleared)"


def test_vendor_text_non_empty_returns_value():
    from tools.catalog_hygiene import _vendor_text

    assert _vendor_text("Nike") == "Nike"
    assert _vendor_text("  Nike  ") == "  Nike  "


# =============================================================================
# Story 9.4 — update_product_type
# =============================================================================
#
# Near-twin of Story 9.2 (update_product_vendor). Semantic deltas pinned here:
#   - Empty / whitespace-only product_type is VALID — clears the field. The
#     vendor twin rejects these as errors.
#   - None is REJECTED (signature requires str; this is a runtime guard).
#     The vendor twin accepts None as its canonical "clear" path.
#   - Wire form for clear is `productType: ""` (Shopify treats "" as cleared
#     for this field); vendor's wire form for clear is `vendor: null`.


# ---------- Helpers — Story 9.4 (update_product_type) ----------


def _type_read(pid="123", product_type="Old Type", title="Tee"):
    return {
        "product": {
            "id": f"gid://shopify/Product/{pid}",
            "title": title,
            "productType": product_type,
        }
    }


def _type_read_by_handle(pid="123", product_type="Old Type", title="Tee"):
    return {
        "productByHandle": {
            "id": f"gid://shopify/Product/{pid}",
            "title": title,
            "productType": product_type,
        }
    }


def _type_update_ok(pid="123", product_type="Crewneck Sweatshirt"):
    return {
        "productUpdate": {
            "product": {"id": f"gid://shopify/Product/{pid}", "productType": product_type},
            "userErrors": [],
        }
    }


def _type_update_user_err(field, message):
    return {
        "productUpdate": {
            "product": None,
            "userErrors": [{"field": field, "message": message}],
        }
    }


# ---------- productId resolution ----------


def test_update_product_type_numeric_id_resolved():
    tools, fc = _build(
        [
            _type_read(pid="5234567890", product_type="Old"),
            _type_update_ok(pid="5234567890", product_type="Crewneck Sweatshirt"),
        ]
    )
    out = tools["update_product_type"](
        product_id="5234567890",
        product_type="Crewneck Sweatshirt",
        confirm=True,
    )
    assert fc.calls[0][0] == GET_PRODUCT_TYPE
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/5234567890"}
    assert fc.calls[1][0] == UPDATE_PRODUCT_TYPE
    assert fc.calls[1][1] == {
        "product": {
            "id": "gid://shopify/Product/5234567890",
            "productType": "Crewneck Sweatshirt",
        }
    }
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["preview"] is False
    assert payload["product"]["id"] == "gid://shopify/Product/5234567890"
    assert payload["product"]["productType"] == "Crewneck Sweatshirt"
    assert payload["errors"] == []
    assert out.startswith("Done.")


def test_update_product_type_gid_passthrough():
    gid = "gid://shopify/Product/7777"
    tools, fc = _build([_type_read(pid="7777", product_type="Old"), _type_update_ok(pid="7777")])
    out = tools["update_product_type"](
        product_id=gid, product_type="Crewneck Sweatshirt", confirm=True
    )
    assert fc.calls[0][1] == {"id": gid}
    assert fc.calls[1][1]["product"]["id"] == gid
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["product"]["id"] == gid


def test_update_product_type_handle_resolved():
    tools, fc = _build(
        [_type_read_by_handle(pid="42", product_type="Old"), _type_update_ok(pid="42")]
    )
    out = tools["update_product_type"](
        product_id="cool-tee", product_type="Crewneck Sweatshirt", confirm=True
    )
    assert fc.calls[0][0] == GET_PRODUCT_TYPE_BY_HANDLE
    assert fc.calls[0][1] == {"handle": "cool-tee"}
    assert fc.calls[1][1]["product"]["id"] == "gid://shopify/Product/42"
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["product"]["id"] == "gid://shopify/Product/42"


def test_update_product_type_handle_not_found():
    tools, fc = _build([{"productByHandle": None}])
    out = tools["update_product_type"](
        product_id="nope-handle", product_type="Crewneck Sweatshirt", confirm=True
    )
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_TYPE_BY_HANDLE
    assert "no product found" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"]
    assert payload["preview"] is False


def test_update_product_type_handle_not_found_dry_run():
    # Symmetric to ..._handle_not_found but with confirm=False. Pins that the
    # not-found error path emits preview=False regardless of confirm.
    tools, fc = _build([{"productByHandle": None}])
    out = tools["update_product_type"](
        product_id="nope-handle", product_type="Crewneck Sweatshirt", confirm=False
    )
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_TYPE_BY_HANDLE
    assert "no product found" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["preview"] is False
    assert payload["errors"][0]["stage"] == "product-resolve"
    assert UPDATE_PRODUCT_TYPE not in [c[0] for c in fc.calls]


def test_update_product_type_numeric_id_not_found():
    tools, fc = _build([{"product": None}])
    out = tools["update_product_type"](
        product_id="999999", product_type="Crewneck Sweatshirt", confirm=False
    )
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_TYPE
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["preview"] is False
    assert payload["errors"][0]["stage"] == "product-resolve"


def test_update_product_type_resolve_exception_surfaces_as_error():
    tools, fc = _build([RuntimeError("transport boom")])
    out = tools["update_product_type"](
        product_id="123", product_type="Crewneck Sweatshirt", confirm=True
    )
    assert len(fc.calls) == 1
    assert "Error resolving product_id (RuntimeError)" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"] == [{"message": "transport boom", "stage": "product-resolve"}]


def test_update_product_type_empty_product_id_fast_fails_without_network():
    tools, fc = _build([])
    out = tools["update_product_type"](
        product_id="", product_type="Crewneck Sweatshirt", confirm=True
    )
    assert fc.calls == []
    assert "product_id must be a non-empty string" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "product-resolve"


def test_update_product_type_empty_gid_body_fast_fails_without_network():
    tools, fc = _build([])
    out = tools["update_product_type"](
        product_id="gid://shopify/Product/",
        product_type="Crewneck Sweatshirt",
        confirm=True,
    )
    assert fc.calls == []
    assert "Empty product GID body" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"][0]["stage"] == "product-resolve"


# ---------- productType validation ----------


def test_update_product_type_rejects_none():
    # None is not a valid clear path — caller must pass ''. Defense-in-depth
    # for runtime callers that bypass the `str` type hint.
    tools, fc = _build([])
    out = tools["update_product_type"](product_id="123", product_type=None, confirm=True)
    assert fc.calls == [], "None product_type must short-circuit before any Shopify call"
    assert "Error:" in out
    assert "required" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"][0]["field"] == "product_type"
    assert payload["errors"][0]["stage"] == "validation"


def test_update_product_type_rejects_over_255_chars():
    tools, fc = _build([])
    long_type = "a" * (PRODUCT_TYPE_MAX_LEN + 1)
    out = tools["update_product_type"](product_id="123", product_type=long_type, confirm=True)
    assert fc.calls == [], ">255-char product_type must reject before any Shopify call"
    assert f"{PRODUCT_TYPE_MAX_LEN}" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["preview"] is False


def test_update_product_type_accepts_exact_255_chars():
    product_type = "a" * PRODUCT_TYPE_MAX_LEN
    tools, fc = _build(
        [
            _type_read(pid="55", product_type="Old"),
            _type_update_ok(pid="55", product_type=product_type),
        ]
    )
    out = tools["update_product_type"](product_id="55", product_type=product_type, confirm=True)
    assert fc.calls[1][0] == UPDATE_PRODUCT_TYPE
    assert fc.calls[1][1]["product"]["productType"] == product_type
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["product"]["productType"] == product_type


def test_update_product_type_trims_whitespace():
    tools, fc = _build(
        [
            _type_read(pid="33", product_type="Old"),
            _type_update_ok(pid="33", product_type="Crewneck"),
        ]
    )
    out = tools["update_product_type"](product_id="33", product_type="  Crewneck  ", confirm=True)
    sent = fc.calls[1][1]["product"]["productType"]
    assert sent == "Crewneck", f"expected trimmed 'Crewneck', got {sent!r}"
    payload = _extract_json(out)
    assert payload["product"]["productType"] == "Crewneck"


# ---------- clearing semantics ----------


def test_update_product_type_empty_string_clears_the_field():
    # Inverse of vendor: empty string is VALID and clears the field. Wire form
    # is `productType: ""` (Shopify treats this as cleared for productType).
    tools, fc = _build(
        [
            _type_read(pid="88", product_type="Crewneck"),
            {
                "productUpdate": {
                    "product": {"id": "gid://shopify/Product/88", "productType": ""},
                    "userErrors": [],
                }
            },
        ]
    )
    out = tools["update_product_type"](product_id="88", product_type="", confirm=True)
    assert fc.calls[1][1]["product"]["productType"] == ""
    assert "(cleared)" in out
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["product"]["productType"] == ""


def test_update_product_type_whitespace_only_clears_the_field():
    # Whitespace-only trims to "" — also a valid clear, NOT a validation error.
    tools, fc = _build(
        [
            _type_read(pid="88", product_type="Crewneck"),
            {
                "productUpdate": {
                    "product": {"id": "gid://shopify/Product/88", "productType": ""},
                    "userErrors": [],
                }
            },
        ]
    )
    out = tools["update_product_type"](product_id="88", product_type="   ", confirm=True)
    assert fc.calls[1][1]["product"]["productType"] == ""
    assert "(cleared)" in out
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["product"]["productType"] == ""


# ---------- dry-run / preview ----------


def test_update_product_type_dry_run_no_mutation():
    tools, fc = _build([_type_read(pid="123", product_type="Old")])
    out = tools["update_product_type"](product_id="123", product_type="Crewneck", confirm=False)
    assert out.startswith("PREVIEW —"), out
    assert "Reply with confirm=True" in out
    assert len(fc.calls) == 1, "preview must not issue UPDATE_PRODUCT_TYPE"
    assert fc.calls[0][0] == GET_PRODUCT_TYPE
    payload = _extract_json(out)
    assert payload["preview"] is True
    assert payload["ok"] is True
    assert payload["product"]["productType"] == "Crewneck"


# ---------- idempotency ----------


def test_update_product_type_idempotent_when_already_set():
    tools, fc = _build([_type_read(pid="123", product_type="Crewneck")])
    out = tools["update_product_type"](product_id="123", product_type="Crewneck", confirm=True)
    assert len(fc.calls) == 1, "idempotent path must skip the mutation"
    assert "no-op" in out
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["preview"] is False
    assert payload["product"]["productType"] == "Crewneck"


def test_update_product_type_idempotent_when_both_empty():
    # Current is "" (or null), target is "" — clear-when-already-clear is a no-op.
    tools, fc = _build([_type_read(pid="123", product_type="")])
    out = tools["update_product_type"](product_id="123", product_type="", confirm=True)
    assert len(fc.calls) == 1
    assert "no-op" in out
    assert "(cleared)" in out
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["preview"] is False
    assert payload["product"]["productType"] == ""


def test_update_product_type_idempotent_when_current_null_target_empty():
    # Shopify may return productType: null for a never-set field. Target is ""
    # — both normalize to "", so this is a no-op.
    tools, fc = _build([_type_read(pid="123", product_type=None)])
    out = tools["update_product_type"](product_id="123", product_type="", confirm=True)
    assert len(fc.calls) == 1
    assert "no-op" in out
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["preview"] is False


def test_update_product_type_idempotent_when_current_whitespace_target_empty():
    # Current is whitespace, target is empty — both normalize to "".
    tools, fc = _build([_type_read(pid="123", product_type="   ")])
    out = tools["update_product_type"](product_id="123", product_type="", confirm=True)
    assert len(fc.calls) == 1
    payload = _extract_json(out)
    assert payload["ok"] is True
    assert payload["preview"] is False


# ---------- error surfacing ----------


def test_update_product_type_user_errors_surface_in_output():
    tools, fc = _build(
        [
            _type_read(pid="123", product_type="Old"),
            _type_update_user_err("productType", "productType is too long"),
        ]
    )
    out = tools["update_product_type"](product_id="123", product_type="Crewneck", confirm=True)
    assert fc.calls[1][0] == UPDATE_PRODUCT_TYPE
    assert "productType is too long" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"] == [{"field": "productType", "message": "productType is too long"}]
    assert payload["preview"] is False


def test_update_product_type_mutation_exception_surfaced():
    tools, fc = _build([_type_read(pid="123", product_type="Old"), RuntimeError("HTTP 503")])
    out = tools["update_product_type"](product_id="123", product_type="Crewneck", confirm=True)
    assert "Error calling productUpdate (RuntimeError)" in out
    payload = _extract_json(out)
    assert payload["ok"] is False
    assert payload["errors"] == [{"message": "HTTP 503", "stage": "product-update"}]


# ---------- JSON tail contract ----------


def test_update_product_type_returns_json_tail_block():
    tools, _fc = _build(
        [
            _type_read(pid="123", product_type="Old"),
            _type_update_ok(pid="123", product_type="Crewneck"),
        ]
    )
    out = tools["update_product_type"](product_id="123", product_type="Crewneck", confirm=True)
    assert "```json\n" in out
    payload = _extract_json(out)
    assert set(payload.keys()) == {"ok", "product", "errors", "preview"}
    assert set(payload["product"].keys()) == {"id", "productType"}


def test_update_product_type_post_mutation_value_echoed():
    # If Shopify normalizes the productType (e.g. trims further), the JSON tail
    # MUST reflect what Shopify stored, not what we sent.
    tools, _fc = _build(
        [
            _type_read(pid="123", product_type="Old"),
            _type_update_ok(pid="123", product_type="CrewneckNormalized"),
        ]
    )
    out = tools["update_product_type"](product_id="123", product_type="Crewneck", confirm=True)
    payload = _extract_json(out)
    assert payload["product"]["productType"] == "CrewneckNormalized"


def test_update_product_type_post_mutation_missing_field_falls_back():
    # Defensive: if Shopify's response omits productType, the tail keeps the
    # value we sent so the JSON shape stays consistent.
    tools, _fc = _build(
        [
            _type_read(pid="123", product_type="Old"),
            {
                "productUpdate": {
                    "product": {"id": "gid://shopify/Product/123"},
                    "userErrors": [],
                }
            },
        ]
    )
    out = tools["update_product_type"](product_id="123", product_type="Crewneck", confirm=True)
    payload = _extract_json(out)
    assert payload["product"]["productType"] == "Crewneck"


def test_update_product_type_post_mutation_null_value_normalized_to_empty():
    # When Shopify echoes productType: null (clear succeeded), the tail must
    # show "" rather than null — keeps the type contract (always a string).
    tools, _fc = _build(
        [
            _type_read(pid="123", product_type="Old"),
            {
                "productUpdate": {
                    "product": {
                        "id": "gid://shopify/Product/123",
                        "productType": None,
                    },
                    "userErrors": [],
                }
            },
        ]
    )
    out = tools["update_product_type"](product_id="123", product_type="", confirm=True)
    payload = _extract_json(out)
    assert payload["product"]["productType"] == ""


# ---------- _type_text unit tests ----------


def test_type_text_empty_string_returns_cleared():
    from tools.catalog_hygiene import _type_text

    assert _type_text("") == "(cleared)"


def test_type_text_whitespace_only_returns_cleared():
    from tools.catalog_hygiene import _type_text

    assert _type_text("   ") == "(cleared)"


def test_type_text_non_empty_returns_value():
    from tools.catalog_hygiene import _type_text

    assert _type_text("Crewneck") == "Crewneck"
    assert _type_text("  Crewneck  ") == "  Crewneck  "


# =============================================================================
# Story 9.6 — update_variant_image_binding
# =============================================================================

from tools.catalog_hygiene import (  # noqa: E402
    GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA,
    PRODUCT_VARIANT_APPEND_MEDIA,
    PRODUCT_VARIANT_DETACH_MEDIA,
    _is_already_bound_error,
    _media_node_to_json,
)

_S96_PRODUCT_GID = "gid://shopify/Product/5234567890"
_S96_VARIANT_A = "gid://shopify/ProductVariant/100"
_S96_VARIANT_B = "gid://shopify/ProductVariant/200"
_S96_MEDIA_1 = "gid://shopify/MediaImage/1"
_S96_MEDIA_2 = "gid://shopify/MediaImage/2"
_S96_MEDIA_3 = "gid://shopify/MediaImage/3"
_S96_MEDIA_CROSS = "gid://shopify/MediaImage/999"


def _s96_combined_response(
    media_ids,
    variants,
    *,
    alt_for_media=None,
    preview_url_for_media=None,
    title="Test Product",
):
    """Build a GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA response.

    variants: list of (variant_gid, sku, [bound_media_ids]).
    """
    alt_for_media = alt_for_media or {}
    preview_url_for_media = preview_url_for_media or {}
    media_nodes = [
        {
            "id": mid,
            "alt": alt_for_media.get(mid),
            "mediaContentType": "IMAGE",
            "image": {"url": preview_url_for_media.get(mid)}
            if mid in preview_url_for_media
            else None,
        }
        for mid in media_ids
    ]
    variant_nodes = [
        {
            "id": vgid,
            "sku": sku,
            "media": {"nodes": [{"id": m} for m in bound]},
        }
        for vgid, sku, bound in variants
    ]
    return {
        "product": {
            "id": _S96_PRODUCT_GID,
            "title": title,
            "media": {"nodes": media_nodes},
            "variants": {"nodes": variant_nodes},
        }
    }


def _s96_mutation_response(productVariants=None, user_errors=None):
    """Build a PRODUCT_VARIANT_APPEND_MEDIA response.

    productVariants: list of (variant_gid, [(media_id, alt, preview_url), ...]).
    """
    pv = []
    for vgid, media in productVariants or []:
        nodes = []
        for mid, alt, preview_url in media:
            node = {"id": mid, "alt": alt, "mediaContentType": "IMAGE"}
            if preview_url is not None:
                node["image"] = {"url": preview_url}
            nodes.append(node)
        pv.append({"id": vgid, "media": {"nodes": nodes}})
    return {
        "productVariantAppendMedia": {
            "productVariants": pv,
            "userErrors": user_errors or [],
        }
    }


def _s96_detach_response(user_errors=None):
    """Build a PRODUCT_VARIANT_DETACH_MEDIA response."""
    return {
        "productVariantDetachMedia": {
            "product": {"id": _S96_PRODUCT_GID},
            "userErrors": user_errors or [],
        }
    }


# ---------- input validation (no network) ----------


def test_s96_empty_product_id_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id="", variant_media=[{"variantId": "1", "mediaIds": [_S96_MEDIA_1]}]
    )
    assert out.startswith("Error: provide product_id")
    assert fc.calls == []
    tail = _parse_tail(out)
    assert tail["ok"] is False


def test_s96_wrong_gid_type_product_id_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id="gid://shopify/Order/1",
        variant_media=[{"variantId": "1", "mediaIds": [_S96_MEDIA_1]}],
    )
    assert out.startswith("Error: provide product_id")
    assert fc.calls == []


def test_s96_empty_variant_media_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](product_id=_S96_PRODUCT_GID, variant_media=[])
    assert "variant_media must be a non-empty list" in out
    assert fc.calls == []


def test_s96_none_variant_media_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](product_id=_S96_PRODUCT_GID, variant_media=None)
    assert "variant_media must be a non-empty list" in out
    assert fc.calls == []


def test_s96_non_dict_entry_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID, variant_media=["not-a-dict"]
    )
    assert "variant_media[0] must be an object" in out
    assert fc.calls == []


def test_s96_missing_variant_id_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"mediaIds": [_S96_MEDIA_1]}],
    )
    assert "variant_media[0].variantId must be a non-empty string" in out
    assert fc.calls == []


def test_s96_non_string_variant_id_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": 42, "mediaIds": [_S96_MEDIA_1]}],
    )
    assert "variant_media[0].variantId must be a non-empty string" in out


def test_s96_whitespace_variant_id_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "   ", "mediaIds": [_S96_MEDIA_1]}],
    )
    assert "variantId must be a non-empty string" in out


def test_s96_empty_media_ids_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "1", "mediaIds": []}],
    )
    assert "variant_media[0].mediaIds must be a non-empty list" in out


def test_s96_non_list_media_ids_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "1", "mediaIds": "not-a-list"}],
    )
    assert "mediaIds must be a non-empty list" in out


def test_s96_malformed_media_gid_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "1", "mediaIds": ["42"]}],
    )
    assert "must be a Shopify media GID" in out


def test_s96_non_string_media_gid_rejected():
    tools, fc = _build([])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "1", "mediaIds": [123]}],
    )
    assert "must be a Shopify media GID" in out


# ---------- resolver + product-not-found ----------


def test_s96_resolver_value_error_returned_as_error():
    # Resolution now runs in-memory against the combined query's variants list
    # (Story 9.3's `resolve_variant_ids_with_variants` enabler). An unknown SKU
    # surfaces the resolver's ValueError without a second fetch.
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "OTHER", [])],
    )
    tools, fc = _build([combined])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "MISSING-SKU", "mediaIds": [_S96_MEDIA_1]}],
    )
    assert out.startswith("Error:")
    assert "MISSING-SKU" in out
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA


def test_s96_product_not_found_message():
    tools, fc = _build([{"product": None}])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
    )
    assert out.startswith(f"No product found with id {_S96_PRODUCT_GID}.")
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["errors"] == [{"message": f"No product found with id {_S96_PRODUCT_GID}."}]


# ---------- cross-product media GID rejection (AC #4) ----------


def test_s96_cross_product_media_gid_rejected():
    tools, fc = _build(
        [
            _s96_combined_response(
                media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
                variants=[(_S96_VARIANT_A, "SKU-A", [])],
            )
        ]
    )
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_CROSS]}],
    )
    assert out.startswith("Error: media GIDs not on product")
    assert _S96_MEDIA_CROSS in out
    assert len(fc.calls) == 1


def test_s96_multiple_unknown_media_gids_listed_once_each():
    other = "gid://shopify/MediaImage/888"
    tools, fc = _build(
        [
            _s96_combined_response(
                media_ids=[_S96_MEDIA_1],
                variants=[(_S96_VARIANT_A, "SKU-A", [])],
            )
        ]
    )
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_CROSS, other, _S96_MEDIA_CROSS]},
        ],
    )
    human_prefix = out.split("```json")[0]
    assert human_prefix.count(_S96_MEDIA_CROSS) == 1
    assert other in human_prefix


# ---------- preview path ----------


def test_s96_preview_returns_with_confirm_hint_and_no_mutation_call():
    tools, fc = _build(
        [
            _s96_combined_response(
                media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
                variants=[(_S96_VARIANT_A, "SKU-A", [])],
                alt_for_media={_S96_MEDIA_1: "front"},
                preview_url_for_media={_S96_MEDIA_1: "https://cdn/1.jpg"},
            )
        ]
    )
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
    )
    assert "PREVIEW — Bind variant images" in out
    assert "Will append" in out
    assert "net-new media bindings: 2" in out
    assert "Reply with confirm=True" in out or "To apply" in out
    assert len(fc.calls) == 1


def test_s96_preview_with_all_no_op_marks_already_bound():
    tools, fc = _build(
        [
            _s96_combined_response(
                media_ids=[_S96_MEDIA_1],
                variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
            )
        ]
    )
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
    )
    assert "PREVIEW" in out
    assert "Already bound" in out
    assert "net-new media bindings: 0" in out


# ---------- happy path (confirm=True) ----------


def test_s96_confirm_executes_mutation_and_returns_bound_state():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[
            (
                _S96_VARIANT_A,
                [(_S96_MEDIA_1, "front", "https://cdn/1.jpg"), (_S96_MEDIA_2, "back", None)],
            )
        ]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert "CONFIRMED — Bind variant images" in out
    assert len(fc.calls) == 2
    assert fc.calls[0][0] == GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA
    assert fc.calls[1][0] == PRODUCT_VARIANT_APPEND_MEDIA
    # Fix A: each mediaId must be its own entry (Shopify enforces exactly 1 per entry).
    sent = fc.calls[1][1]
    assert sent["productId"] == _S96_PRODUCT_GID
    assert len(sent["variantMedia"]) == 2
    assert all(e["variantId"] == _S96_VARIANT_A for e in sent["variantMedia"])
    assert all(len(e["mediaIds"]) == 1 for e in sent["variantMedia"])


def test_s96_numeric_variant_id_is_coerced_to_gid_without_extra_fetch():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[("gid://shopify/ProductVariant/42", "", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[("gid://shopify/ProductVariant/42", [(_S96_MEDIA_1, "", None)])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "42", "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED" in out
    assert len(fc.calls) == 2
    assert fc.calls[0][0] == GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA


def test_s96_resolves_sku_against_combined_query_variants_no_extra_fetch():
    # SKU input is resolved against the combined query's variants list — no
    # separate resolver fetch. Total round-trips: 2 (combined + mutation),
    # not 3.
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "", None)])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "SKU-A", "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED" in out
    assert len(fc.calls) == 2
    assert fc.calls[0][0] == GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA
    assert fc.calls[1][0] == PRODUCT_VARIANT_APPEND_MEDIA
    # Mutation uses the RESOLVED gid, not the SKU string.
    assert fc.calls[1][1]["variantMedia"][0]["variantId"] == _S96_VARIANT_A


def test_s96_combined_query_variants_flow_through_to_resolver(monkeypatch):
    """Pin the resolver-of-record contract.

    The renamed `..._no_extra_fetch` test asserts on the call count (2 vs 3)
    and the resolved GID landing in the mutation, which is sufficient
    behavioral evidence today. This test goes further and pins WHICH resolver
    is called and WHAT it's called with — specifically that the variants list
    flows from the combined query straight into
    `resolve_variant_ids_with_variants` without an interceding fetch.

    A future refactor that swapped the enabler for a fresh fetch would still
    pass the count-based test (same number of `client.execute` calls if the
    refactor happens to balance) but would fail this one.
    """
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "", None)])]
    )

    captured: dict[str, Any] = {}
    original = catalog_hygiene.resolve_variant_ids_with_variants

    def _spy(
        variant_ids: list[str], variants: list[dict[str, Any]], *, product_gid: str
    ) -> list[str]:
        captured["variant_ids"] = list(variant_ids)
        captured["variants"] = list(variants)
        captured["product_gid"] = product_gid
        return original(variant_ids, variants, product_gid=product_gid)

    monkeypatch.setattr(catalog_hygiene, "resolve_variant_ids_with_variants", _spy)

    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "SKU-A", "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED" in out
    # Resolver received the variants list straight from the combined query.
    assert captured["product_gid"] == _S96_PRODUCT_GID
    assert captured["variant_ids"] == ["SKU-A"]
    assert captured["variants"] == [{"id": _S96_VARIANT_A, "sku": "SKU-A", "media": {"nodes": []}}]


# ---------- idempotent no-op (AC #6) ----------


def test_s96_idempotent_no_op_when_all_media_already_bound():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1, _S96_MEDIA_2])],
    )
    tools, fc = _build([combined])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert "CONFIRMED — Bind variant images (no-op)" in out
    assert len(fc.calls) == 1
    tail = _parse_tail(out)
    assert tail["ok"] is True
    media_ids_in_tail = {m["id"] for m in tail["variants"][0]["media"]}
    assert media_ids_in_tail == {_S96_MEDIA_1, _S96_MEDIA_2}


# ---------- duplicate variantId merge ----------


def test_s96_duplicate_variant_id_entries_merge_to_single_mutation_entry():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "", None), (_S96_MEDIA_2, "", None)])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]},
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_2]},
        ],
        confirm=True,
    )
    assert "CONFIRMED" in out
    # Fix A: duplicate variant entries are merged (Step 5a) then expanded to one
    # entry per mediaId. Two distinct mediaIds → 2 append entries.
    sent = fc.calls[1][1]["variantMedia"]
    assert len(sent) == 2
    assert all(e["variantId"] == _S96_VARIANT_A for e in sent)
    assert all(len(e["mediaIds"]) == 1 for e in sent)
    appended_mids = {e["mediaIds"][0] for e in sent}
    assert appended_mids == {_S96_MEDIA_1, _S96_MEDIA_2}


def test_s96_duplicate_variant_id_with_overlapping_media_dedups():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "", None), (_S96_MEDIA_2, "", None)])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]},
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]},  # overlap
        ],
        confirm=True,
    )
    assert "CONFIRMED" in out
    # Fix A: overlapping input entries merge (Step 5a dedup) to {M1, M2}, then
    # each gets its own single-mediaId append entry.
    sent = fc.calls[1][1]["variantMedia"]
    assert len(sent) == 2
    appended_mids = {e["mediaIds"][0] for e in sent}
    assert appended_mids == {_S96_MEDIA_1, _S96_MEDIA_2}


def test_s96_duplicate_variant_id_all_already_bound_collapses_to_no_op():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    tools, fc = _build([combined])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]},
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]},
        ],
        confirm=True,
    )
    assert "CONFIRMED — Bind variant images (no-op)" in out
    assert len(fc.calls) == 1


# ---------- partial overlap → only delta sent ----------


def test_s96_partial_overlap_routes_to_detach_reattach():
    # Variant has M1 already bound; desired is [M1, M2, M3]. Since current is
    # non-empty and current != desired, the detach-reattach path fires: detach
    # M1 first, then reattach all three (M1 included). Fix B + Fix A.
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    detach = _s96_detach_response()
    mutation = _s96_mutation_response(
        productVariants=[
            (
                _S96_VARIANT_A,
                [(_S96_MEDIA_1, "", None), (_S96_MEDIA_2, "", None), (_S96_MEDIA_3, "", None)],
            )
        ]
    )
    tools, fc = _build([combined, detach, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3]}
        ],
        confirm=True,
    )
    assert "CONFIRMED" in out
    assert len(fc.calls) == 3
    assert fc.calls[1][0] == PRODUCT_VARIANT_DETACH_MEDIA
    assert fc.calls[2][0] == PRODUCT_VARIANT_APPEND_MEDIA
    # Detach carries the existing binding; append carries all 3 as separate entries.
    det_sent = fc.calls[1][1]["variantMedia"]
    assert _S96_MEDIA_1 in det_sent[0]["mediaIds"]
    app_sent = fc.calls[2][1]["variantMedia"]
    assert len(app_sent) == 3
    assert all(len(e["mediaIds"]) == 1 for e in app_sent)


def test_s96_duplicate_media_ids_within_entry_are_deduped():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "", None)])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_1, _S96_MEDIA_1]}
        ],
        confirm=True,
    )
    assert "CONFIRMED" in out
    assert fc.calls[1][1]["variantMedia"] == [
        {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}
    ]


def test_s96_mixed_no_op_and_delta_variants_only_sends_delta_variants():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[
            (_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1]),
            (_S96_VARIANT_B, "SKU-B", []),
        ],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_B, [(_S96_MEDIA_2, "", None)])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]},
            {"variantId": _S96_VARIANT_B, "mediaIds": [_S96_MEDIA_2]},
        ],
        confirm=True,
    )
    assert "CONFIRMED" in out
    assert fc.calls[1][1]["variantMedia"] == [
        {"variantId": _S96_VARIANT_B, "mediaIds": [_S96_MEDIA_2]}
    ]
    # Both variants render in the JSON tail — no-op via fallback, delta via post-state.
    tail = _parse_tail(out)
    ids_in_tail = [v["id"] for v in tail["variants"]]
    assert ids_in_tail == [_S96_VARIANT_A, _S96_VARIANT_B]


def test_s96_post_mutation_variant_with_empty_media_renders_empty_in_tail():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(productVariants=[(_S96_VARIANT_A, [])])
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED" in out
    tail = _parse_tail(out)
    assert tail["variants"] == [{"id": _S96_VARIANT_A, "sku": "SKU-A", "media": []}]


# ---------- userErrors handling ----------


def test_s96_user_errors_already_bound_treated_as_success():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "", None)])],
        user_errors=[{"field": ["variantMedia"], "message": "Media is already bound to variant"}],
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED" in out
    assert not out.split("```json")[0].startswith("Error:")


def test_s96_user_errors_other_returned_as_error():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        user_errors=[{"field": ["variantMedia"], "message": "Media is not ready"}],
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "Media is not ready" in out
    # Human prefix uses dotted-path field formatting.
    assert "variantMedia:" in out.split("```json")[0]


def test_s96_user_errors_mixed_real_and_already_bound_returns_real_only():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        user_errors=[
            {"field": ["variantMedia", "0"], "message": "Media is already associated"},
            {"field": ["variantMedia", "1"], "message": "Media is not ready"},
        ],
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert out.startswith("Error:")
    assert "not ready" in out
    assert "already associated" not in out


def test_s96_user_errors_only_already_bound_with_no_returned_variants_falls_through_to_success():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = {
        "productVariantAppendMedia": {
            "productVariants": None,
            "userErrors": [{"field": None, "message": "Already bound"}],
        }
    }
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED — Bind variant images" in out
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["variants"] == [{"id": _S96_VARIANT_A, "sku": "SKU-A", "media": []}]


# ---------- defensive paths ----------


def test_s96_runtime_error_propagates():
    tools, fc = _build([RuntimeError("Shopify HTTP error: 503")])
    with pytest.raises(RuntimeError):
        tools["update_variant_image_binding"](
            product_id=_S96_PRODUCT_GID,
            variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
            confirm=True,
        )


def test_s96_variant_node_without_id_is_skipped():
    combined = {
        "product": {
            "id": _S96_PRODUCT_GID,
            "title": "T",
            "media": {"nodes": [{"id": _S96_MEDIA_1, "alt": None, "mediaContentType": "IMAGE"}]},
            "variants": {
                "nodes": [
                    {"id": None, "sku": "GHOST", "media": {"nodes": []}},
                    {"id": _S96_VARIANT_A, "sku": "SKU-A", "media": {"nodes": []}},
                ]
            },
        }
    }
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "", None)])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED" in out


def test_s96_mutation_response_with_returned_variant_missing_id_is_skipped():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = {
        "productVariantAppendMedia": {
            "productVariants": [
                {"id": None, "media": {"nodes": []}},
                {"id": _S96_VARIANT_A, "media": {"nodes": [{"id": _S96_MEDIA_1}]}},
            ],
            "userErrors": [],
        }
    }
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED" in out
    tail = _parse_tail(out)
    assert tail["variants"][0]["media"][0]["id"] == _S96_MEDIA_1


def test_s96_no_op_confirmed_surfaces_bound_mid_without_image_data():
    # Variant has two bound mediaIds; one (far_mid) has no image data in the
    # product media index. Since desired == current, it's a no-op. The JSON
    # tail must include far_mid with alt=None and image=None via the fallback
    # path through _media_node_to_json.
    far_mid = "gid://shopify/MediaImage/9999"
    combined = {
        "product": {
            "id": _S96_PRODUCT_GID,
            "title": "T",
            "media": {
                "nodes": [
                    {"id": _S96_MEDIA_1, "alt": None, "mediaContentType": "IMAGE"},
                    # far_mid is in the product-level list but has no image key.
                    {"id": far_mid, "alt": None, "mediaContentType": "IMAGE"},
                ]
            },
            "variants": {
                "nodes": [
                    {
                        "id": _S96_VARIANT_A,
                        "sku": "SKU-A",
                        "media": {"nodes": [{"id": far_mid}, {"id": _S96_MEDIA_1}]},
                    }
                ]
            },
        }
    }
    tools, fc = _build([combined])
    # desired == current → no-op path.
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, far_mid]}],
        confirm=True,
    )
    assert "CONFIRMED — Bind variant images (no-op)" in out
    assert len(fc.calls) == 1
    tail = _parse_tail(out)
    ids_in_tail = {m["id"] for m in tail["variants"][0]["media"]}
    assert ids_in_tail == {far_mid, _S96_MEDIA_1}
    # far_mid node has no image key → _media_node_to_json renders image as None.
    far = next(m for m in tail["variants"][0]["media"] if m["id"] == far_mid)
    assert far["alt"] is None
    assert far["image"] is None


# ---------- _is_already_bound_error helper ----------


def test_s96_is_already_bound_error_matches_bound():
    assert _is_already_bound_error("Media is already bound") is True


def test_s96_is_already_bound_error_matches_associated():
    assert _is_already_bound_error("Media is already associated with variant") is True


def test_s96_is_already_bound_error_rejects_other_messages():
    assert _is_already_bound_error("Media is not ready") is False
    assert _is_already_bound_error("") is False
    assert _is_already_bound_error("already in a relationship") is False


# ---------- _media_node_to_json helper ----------


def test_s96_media_node_to_json_full_node():
    out = _media_node_to_json(
        {"id": _S96_MEDIA_1, "alt": "front", "mediaContentType": "IMAGE", "image": {"url": "u"}}
    )
    assert out == {
        "id": _S96_MEDIA_1,
        "alt": "front",
        "mediaContentType": "IMAGE",
        "image": {"url": "u"},
    }


def test_s96_media_node_to_json_missing_image_renders_none():
    out = _media_node_to_json({"id": _S96_MEDIA_1, "alt": None, "mediaContentType": "VIDEO"})
    assert out == {
        "id": _S96_MEDIA_1,
        "alt": None,
        "mediaContentType": "VIDEO",
        "image": None,
    }


# ---------- JSON-tail shape pinning ----------


def test_s96_preview_json_tail_shape_append_only():
    # Variant with 0 existing bindings → append-only path; preview uses wouldAppend.
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    tools, fc = _build([combined])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
    )
    tail = _parse_tail(out)
    assert tail == {
        "ok": True,
        "dryRun": True,
        "variants": [
            {
                "id": _S96_VARIANT_A,
                "sku": "SKU-A",
                "path": "append-only",
                "currentMedia": [],
                "wouldAppend": [_S96_MEDIA_1, _S96_MEDIA_2],
            }
        ],
        "errors": [],
    }


def test_s96_preview_json_tail_shape_detach_reattach():
    # Variant with 1 existing binding and 2 desired → detach-reattach path;
    # preview uses willDetach / willReattach / netNew (Fix C).
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    tools, fc = _build([combined])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
    )
    tail = _parse_tail(out)
    assert tail == {
        "ok": True,
        "dryRun": True,
        "variants": [
            {
                "id": _S96_VARIANT_A,
                "sku": "SKU-A",
                "path": "detach-reattach",
                "currentMedia": [_S96_MEDIA_1],
                "willDetach": [_S96_MEDIA_1],
                "willReattach": [_S96_MEDIA_1, _S96_MEDIA_2],
                "netNew": [_S96_MEDIA_2],
            }
        ],
        "errors": [],
    }


def test_s96_done_json_tail_shape_matches_spec_example():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "front", "https://cdn/1.jpg")])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    tail = _parse_tail(out)
    assert tail == {
        "ok": True,
        "variants": [
            {
                "id": _S96_VARIANT_A,
                "sku": "SKU-A",
                "media": [
                    {
                        "id": _S96_MEDIA_1,
                        "alt": "front",
                        "mediaContentType": "IMAGE",
                        "image": {"url": "https://cdn/1.jpg"},
                    }
                ],
            }
        ],
        "errors": [],
    }


def test_s96_shopify_user_errors_pass_through_verbatim_in_tail():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    user_errors = [{"field": ["variantMedia"], "message": "Media is not ready"}]
    mutation = _s96_mutation_response(user_errors=user_errors)
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["errors"] == user_errors


# =============================================================================
# Story 9.9 — update_variant_image_binding: detach-reattach enhancement
# =============================================================================
#
# AC coverage map:
#   AC1 (Fix A — input expansion)         → T2
#   AC2 (Fix B — detach-reattach)         → T3, T6
#   AC3 (detach-halt)                     → test_s99_detach_userErrors_halts_before_append
#   AC4 (mixed-batch routing)             → T6
#   AC5 (idempotency)                     → T5
#   AC6 (willLose warning)                → T4
#   AC7 (Fix C — dryRun accuracy)         → T8
#   AC8 (partial-failure handling)        → T9a, T9b
#   AC9 (media membership pre-flight)     → T7
#   AC10 (no new MCP tool)               → test_s99_PRODUCT_VARIANT_DETACH_MEDIA_constant_importable
#   AC11 (all tests pass)                 → this section
#   AC12 (CI chain)                       → verified at end of session


def test_s99_zero_existing_one_desired_appends_one_entry():
    """T1 — 0 existing bindings, 1 desired mediaId → standard append path, 1 entry."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, None, None)])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert len(fc.calls) == 2
    assert fc.calls[0][0] == GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA
    assert fc.calls[1][0] == PRODUCT_VARIANT_APPEND_MEDIA
    sent = fc.calls[1][1]["variantMedia"]
    assert len(sent) == 1
    assert sent[0]["variantId"] == _S96_VARIANT_A
    assert sent[0]["mediaIds"] == [_S96_MEDIA_1]
    tail = _parse_tail(out)
    assert tail["ok"] is True


def test_s99_zero_existing_four_desired_expands_to_four_entries():
    """T2 — Fix A: 4 desired mediaIds must produce 4 separate single-mediaId append entries."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3, "gid://shopify/MediaImage/4"],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, None, None)])]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {
                "variantId": _S96_VARIANT_A,
                "mediaIds": [
                    _S96_MEDIA_1,
                    _S96_MEDIA_2,
                    _S96_MEDIA_3,
                    "gid://shopify/MediaImage/4",
                ],
            }
        ],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert len(fc.calls) == 2
    sent = fc.calls[1][1]["variantMedia"]
    # Every entry must carry exactly one mediaId — the core of Fix A.
    assert len(sent) == 4
    for entry in sent:
        assert entry["variantId"] == _S96_VARIANT_A
        assert len(entry["mediaIds"]) == 1


def test_s99_one_existing_four_desired_detach_then_reattach():
    """T3 — Fix B: 1 existing binding → detach first, then reattach all 4."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3, "gid://shopify/MediaImage/4"],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    detach = _s96_detach_response()
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, None, None)])]
    )
    tools, fc = _build([combined, detach, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {
                "variantId": _S96_VARIANT_A,
                "mediaIds": [
                    _S96_MEDIA_1,
                    _S96_MEDIA_2,
                    _S96_MEDIA_3,
                    "gid://shopify/MediaImage/4",
                ],
            }
        ],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert len(fc.calls) == 3
    assert fc.calls[0][0] == GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA
    assert fc.calls[1][0] == PRODUCT_VARIANT_DETACH_MEDIA
    assert fc.calls[2][0] == PRODUCT_VARIANT_APPEND_MEDIA
    # Detach entry carries the existing binding (M1)
    det_sent = fc.calls[1][1]["variantMedia"]
    assert len(det_sent) == 1
    assert det_sent[0]["variantId"] == _S96_VARIANT_A
    assert _S96_MEDIA_1 in det_sent[0]["mediaIds"]
    # Append entries must each carry exactly 1 mediaId (Fix A) and cover all 4
    app_sent = fc.calls[2][1]["variantMedia"]
    assert len(app_sent) == 4
    for entry in app_sent:
        assert len(entry["mediaIds"]) == 1
    tail = _parse_tail(out)
    assert tail["ok"] is True


def test_s99_dryrun_willlose_shows_warning():
    """T4 (dryRun half) — existing binding not in desired set → willLose present, no mutations."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    tools, fc = _build([combined])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_2, _S96_MEDIA_3]}],
        confirm=False,
    )
    assert out.startswith("PREVIEW —"), out
    assert len(fc.calls) == 1
    tail = _parse_tail(out)
    assert tail["dryRun"] is True
    v = tail["variants"][0]
    assert v["path"] == "detach-reattach"
    assert _S96_MEDIA_1 in v["willLose"]


def test_s99_confirm_detach_reattach_drops_old_image():
    """T4 (confirm half) — existing NOT in desired → detach removes it, append uses caller's set."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    detach = _s96_detach_response()
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_2, None, None), (_S96_MEDIA_3, None, None)])]
    )
    tools, fc = _build([combined, detach, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_2, _S96_MEDIA_3]}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert len(fc.calls) == 3
    # Detach must include M1 (the dropped binding)
    det_sent = fc.calls[1][1]["variantMedia"]
    assert _S96_MEDIA_1 in det_sent[0]["mediaIds"]
    # Append must NOT include M1
    app_sent = fc.calls[2][1]["variantMedia"]
    app_media_ids = [e["mediaIds"][0] for e in app_sent]
    assert _S96_MEDIA_1 not in app_media_ids
    assert _S96_MEDIA_2 in app_media_ids
    assert _S96_MEDIA_3 in app_media_ids
    tail = _parse_tail(out)
    assert tail["ok"] is True


def test_s99_exact_set_match_is_noop():
    """T5 — idempotency: variant already has exactly the desired set → no mutations."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1, _S96_MEDIA_2])],
    )
    tools, fc = _build([combined])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED — Bind variant images (no-op)"), out
    assert len(fc.calls) == 1  # read only — no detach, no append
    tail = _parse_tail(out)
    assert tail["ok"] is True


def test_s99_mixed_batch_one_append_only_one_detach_reattach():
    """T6 — mixed batch: V_A clean (append-only), V_B dirty (detach-reattach).
    Detach is issued for V_B only; single append batches both V_A and V_B.
    """
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3],
        variants=[
            (_S96_VARIANT_A, "SKU-A", []),
            (_S96_VARIANT_B, "SKU-B", [_S96_MEDIA_1]),
        ],
    )
    detach = _s96_detach_response()
    mutation = _s96_mutation_response(
        productVariants=[
            (_S96_VARIANT_A, [(_S96_MEDIA_2, None, None)]),
            (_S96_VARIANT_B, [(_S96_MEDIA_1, None, None), (_S96_MEDIA_3, None, None)]),
        ]
    )
    tools, fc = _build([combined, detach, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_2]},
            {"variantId": _S96_VARIANT_B, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_3]},
        ],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    assert len(fc.calls) == 3
    assert fc.calls[1][0] == PRODUCT_VARIANT_DETACH_MEDIA
    assert fc.calls[2][0] == PRODUCT_VARIANT_APPEND_MEDIA
    # Detach must only target V_B
    det_sent = fc.calls[1][1]["variantMedia"]
    assert len(det_sent) == 1
    assert det_sent[0]["variantId"] == _S96_VARIANT_B
    # Append must cover entries for both variants in a single call
    app_sent = fc.calls[2][1]["variantMedia"]
    app_variant_ids = {e["variantId"] for e in app_sent}
    assert _S96_VARIANT_A in app_variant_ids
    assert _S96_VARIANT_B in app_variant_ids
    tail = _parse_tail(out)
    assert tail["ok"] is True


def test_s99_media_not_on_product_rejects_preflight():
    """T7 — media membership pre-flight: foreign mediaId rejected before any mutation."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    tools, fc = _build([combined])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_CROSS]}],
        confirm=True,
    )
    assert out.startswith("Error:"), out
    assert len(fc.calls) == 1  # read only, no mutation
    tail = _parse_tail(out)
    assert tail["ok"] is False


def test_s99_dryrun_detach_reattach_payload_shape():
    """T8 — Fix C: dryRun on detach-reattach variant returns correct path-aware shape."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    tools, fc = _build([combined])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3]}
        ],
        confirm=False,
    )
    assert out.startswith("PREVIEW —"), out
    assert len(fc.calls) == 1  # no mutations during dryRun
    tail = _parse_tail(out)
    assert tail["dryRun"] is True
    v = tail["variants"][0]
    assert v["path"] == "detach-reattach"
    assert v["willDetach"] == [_S96_MEDIA_1]
    assert v["willReattach"] == [_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3]
    assert v["netNew"] == [_S96_MEDIA_2, _S96_MEDIA_3]


def test_s99_detach_ok_append_fails_rollback_succeeds():
    """T9a — partial failure: detach OK, append fails, rollback succeeds → ok:false, zeroMediaVariants empty."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    detach = _s96_detach_response()
    append_fail = _s96_mutation_response(user_errors=[{"field": [], "message": "Media not found"}])
    rollback_ok = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, None, None)])]
    )
    tools, fc = _build([combined, detach, append_fail, rollback_ok])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert out.startswith("Error:"), out
    assert len(fc.calls) == 4
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["appendFailedAfterDetach"] is True
    assert tail["rollbackOk"] is True
    assert tail["zeroMediaVariants"] == []


def test_s99_detach_ok_append_fails_rollback_also_fails():
    """T9b — partial failure: detach OK, append fails, rollback also fails → zeroMediaVariants listed."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    detach = _s96_detach_response()
    append_fail = _s96_mutation_response(user_errors=[{"field": [], "message": "Media not found"}])
    rollback_fail = _s96_mutation_response(
        user_errors=[{"field": [], "message": "Rollback also failed"}]
    )
    tools, fc = _build([combined, detach, append_fail, rollback_fail])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert out.startswith("Error:"), out
    assert len(fc.calls) == 4
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["appendFailedAfterDetach"] is True
    assert tail["rollbackOk"] is False
    assert _S96_VARIANT_A in tail["zeroMediaVariants"]


def test_s99_detach_ok_append_fails_rollback_raises_exception():
    """Rollback call itself raises (not userErrors) → exception reported, zeroMediaVariants listed."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    detach = _s96_detach_response()
    append_fail = _s96_mutation_response(user_errors=[{"field": [], "message": "Media not found"}])
    # FakeClient raises when a BaseException instance is queued.
    rollback_raises = RuntimeError("Shopify 503")
    tools, fc = _build([combined, detach, append_fail, rollback_raises])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert out.startswith("Error:"), out
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["rollbackOk"] is False
    assert _S96_VARIANT_A in tail["zeroMediaVariants"]
    assert any("rollback raised" in e.get("message", "") for e in tail["rollbackErrors"])


def test_s99_detach_reattach_variant_missing_from_append_response_uses_desired_fallback():
    """Detach-reattach succeeds but variant missing from productVariants response → desired fallback."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    detach = _s96_detach_response()
    # Append returns empty productVariants (variant missing from response).
    mutation_no_variants = {"productVariantAppendMedia": {"productVariants": [], "userErrors": []}}
    tools, fc = _build([combined, detach, mutation_no_variants])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —"), out
    tail = _parse_tail(out)
    assert tail["ok"] is True
    # Fallback synthesises from desired set via product_media_index.
    ids_in_tail = {m["id"] for m in tail["variants"][0]["media"]}
    assert _S96_MEDIA_1 in ids_in_tail
    assert _S96_MEDIA_2 in ids_in_tail


def test_s99_detach_userErrors_halts_before_append():
    """Regression — detach returns userErrors: append must never be called."""
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    detach_err = _s96_detach_response(
        user_errors=[{"field": [], "message": "Cannot detach media from this variant"}]
    )
    # Only 2 responses queued — FakeClient will raise if a 3rd call is made.
    tools, fc = _build([combined, detach_err])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert out.startswith("Error:"), out
    assert len(fc.calls) == 2
    assert fc.calls[1][0] == PRODUCT_VARIANT_DETACH_MEDIA
    tail = _parse_tail(out)
    assert tail["ok"] is False


def test_s99_log_write_called_with_detach_and_append_counts(monkeypatch):
    """Regression — log_write must include detached=N appended=M in the new format."""
    import tools.catalog_hygiene as _ch

    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(_ch, "log_write", lambda name, msg: logged.append((name, msg)))

    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    detach = _s96_detach_response()
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, None, None), (_S96_MEDIA_2, None, None)])]
    )
    tools, _ = _build([combined, detach, mutation])
    tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
        confirm=True,
    )
    assert logged, "log_write was never called"
    _, msg = logged[-1]
    assert "detached=" in msg
    assert "appended=" in msg


def test_s99_PRODUCT_VARIANT_DETACH_MEDIA_constant_importable():
    """Regression — guards against accidental rename of the module-level constant."""
    from tools.catalog_hygiene import PRODUCT_VARIANT_DETACH_MEDIA as _c

    assert "productVariantDetachMedia" in _c


# =============================================================================
# Story 9.7 — set_product_metafields
# =============================================================================
#
# AC coverage map (see context/EPIC_9_catalog_hygiene_stories.md §Story 9.7):
#   AC #1  25-entry-per-call cap        → test_s97_validation_rejects param
#   AC #2  required keys per entry      → test_s97_validation_rejects param
#   AC #3  type/value shape checks      → test_s97_value_shape_* tests
#   AC #4  reserved namespace reject    → test_s97_validation_rejects param
#   AC #5  metafieldsSet single call    → test_s97_mutation_*_path tests
#   AC #6  per-entry errors by index    → test_s97_user_errors_indexed_by_entry
#   AC #7  dry-run path (confirm=False) → test_s97_preview_does_not_call_mutation
#   AC #8  userErrors verbatim          → test_s97_user_errors_indexed_by_entry
#   AC #9  idempotent re-run            → test_s97_idempotent_rerun_succeeds_twice
#   AC #10 ACCESS_DENIED + remediation  → test_s97_access_denied_* tests
#   AC #11 success return shape         → test_s97_success_json_tail_shape

_S97_PRODUCT_GID = "gid://shopify/Product/777"
_S97_VARIANT_GID = "gid://shopify/ProductVariant/8001"
_S97_METAFIELD_GID = "gid://shopify/Metafield/42"
_S97_METAFIELD_GID_2 = "gid://shopify/Metafield/43"


def _s97_entry(
    *,
    owner_id: str = _S97_PRODUCT_GID,
    namespace: str = "custom",
    key: str = "fabric_weight_oz",
    value: str = "14",
    mtype: str = "number_integer",
) -> dict:
    return {
        "ownerId": owner_id,
        "namespace": namespace,
        "key": key,
        "value": value,
        "type": mtype,
    }


def _s97_mutation_response(metafields=None, user_errors=None):
    return {
        "metafieldsSet": {
            "metafields": metafields or [],
            "userErrors": user_errors or [],
        }
    }


# --- Validation rejects (no network) -----------------------------------------


@pytest.mark.parametrize(
    "metafields,fragment",
    [
        (None, "metafields must be a non-empty list"),
        ([], "metafields must be a non-empty list"),
        ("not-a-list", "metafields must be a non-empty list"),
        # 26 entries hits the cap.
        ([_s97_entry()] * 26, "exceeds the 25-entry"),
        (["not-a-dict"], "metafields[0] must be an object"),
        # Missing each required key, one per row.
        ([{**_s97_entry(), "ownerId": None}], "ownerId must be a non-empty string"),
        ([{**_s97_entry(), "ownerId": ""}], "ownerId must be a non-empty string"),
        # Out-of-scope owner type — Collection is not Product or ProductVariant.
        (
            [{**_s97_entry(), "ownerId": "gid://shopify/Collection/1"}],
            "must be a Product or ProductVariant GID",
        ),
        # Wrong shape (not a GID at all).
        (
            [{**_s97_entry(), "ownerId": "12345"}],
            "must be a Product or ProductVariant GID",
        ),
        # Empty body after prefix.
        (
            [{**_s97_entry(), "ownerId": "gid://shopify/Product/"}],
            "ownerId has empty GID body",
        ),
        ([{**_s97_entry(), "namespace": None}], "namespace must be a non-empty string"),
        ([{**_s97_entry(), "namespace": "   "}], "namespace must be a non-empty string"),
        # Reserved namespace.
        (
            [{**_s97_entry(), "namespace": "app--myapp"}],
            "uses the reserved 'app--' prefix",
        ),
        ([{**_s97_entry(), "key": None}], "key must be a non-empty string"),
        ([{**_s97_entry(), "type": None}], "type must be a non-empty string"),
        ([{**_s97_entry(), "value": 14}], "value must be a string"),
        # Type-shape mismatches.
        (
            [{**_s97_entry(), "value": "1.5", "mtype": "number_integer"}],
            "not a valid integer for type 'number_integer'",
        ),
        (
            [{**_s97_entry(), "value": "abc", "mtype": "number_integer"}],
            "not a valid integer for type 'number_integer'",
        ),
        (
            [{**_s97_entry(), "value": "abc", "mtype": "number_decimal"}],
            "not a valid decimal for type 'number_decimal'",
        ),
        (
            [{**_s97_entry(), "value": "yes", "mtype": "boolean"}],
            "must be 'true' or 'false' for type 'boolean'",
        ),
        (
            [{**_s97_entry(), "value": "{not json", "mtype": "json"}],
            "not valid JSON for type 'json'",
        ),
        (
            [
                {
                    **_s97_entry(),
                    "value": "{not json",
                    "mtype": "list.single_line_text_field",
                }
            ],
            "not valid JSON for list-type",
        ),
        (
            [
                {
                    **_s97_entry(),
                    "value": '"a string, not a list"',
                    "mtype": "list.single_line_text_field",
                }
            ],
            "must decode to a JSON array",
        ),
    ],
)
def test_s97_validation_rejects(metafields, fragment):
    # Patch the parametrize indirection: some rows use `mtype=` shorthand.
    if isinstance(metafields, list):
        for e in metafields:
            if isinstance(e, dict) and "mtype" in e:
                e["type"] = e.pop("mtype")

    tools, fc = _build([])
    out = tools["set_product_metafields"](metafields=metafields)
    assert out.startswith("Error:")
    assert fragment in out
    assert fc.calls == []  # No mutation call before validation passes.
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["metafields"] == []
    # Cap / shape errors that short-circuit before per-entry validation use
    # the plain `errors` list (no `errorsByIndex` because there's no index).
    # Per-entry validation failures populate `errorsByIndex`.


def test_s97_validation_aggregates_multiple_errors_per_entry():
    # One entry with TWO problems: out-of-scope owner + bad namespace.
    bad = {
        "ownerId": "gid://shopify/Collection/9",
        "namespace": "app--reserved",
        "key": "k",
        "value": "v",
        "type": "single_line_text_field",
    }
    tools, fc = _build([])
    out = tools["set_product_metafields"](metafields=[bad])
    assert fc.calls == []
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert "0" in tail["errorsByIndex"]
    msgs = " | ".join(e["message"] for e in tail["errorsByIndex"]["0"])
    assert "must be a Product or ProductVariant GID" in msgs
    assert "uses the reserved 'app--' prefix" in msgs


def test_s97_validation_indexes_each_failing_entry_separately():
    # Two entries, both bad — errorsByIndex keys 0 and 1 must both appear.
    bad_a = {**_s97_entry(), "ownerId": "nope"}
    bad_b = {**_s97_entry(), "value": "1.5", "type": "number_integer"}
    tools, fc = _build([])
    out = tools["set_product_metafields"](metafields=[bad_a, bad_b])
    assert fc.calls == []
    tail = _parse_tail(out)
    assert set(tail["errorsByIndex"].keys()) == {"0", "1"}


def test_s97_unknown_type_passes_validation_through_to_shopify():
    # `weight` is not in our curated SUPPORTED_METAFIELD_TYPES — it falls
    # through to Shopify without a client-side shape check.
    weight_entry = _s97_entry(value="3.5", mtype="weight")
    tools, fc = _build(
        [
            _s97_mutation_response(
                metafields=[
                    {
                        "id": _S97_METAFIELD_GID,
                        "namespace": "custom",
                        "key": "fabric_weight_oz",
                        "value": "3.5",
                        "type": "weight",
                        "ownerType": "PRODUCT",
                    }
                ]
            )
        ]
    )
    out = tools["set_product_metafields"](metafields=[weight_entry], confirm=True)
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["metafields"][0]["type"] == "weight"


# --- Dry-run / preview path --------------------------------------------------


def test_s97_preview_does_not_call_mutation():
    tools, fc = _build([])
    out = tools["set_product_metafields"](metafields=[_s97_entry()], confirm=False)
    assert fc.calls == []
    assert "PREVIEW" in out
    assert "To apply, call again with confirm=True" in out
    tail = _parse_tail(out)
    assert tail["preview"] is True
    assert tail["ok"] is True
    assert tail["metafields"][0]["ownerId"] == _S97_PRODUCT_GID
    assert tail["metafields"][0]["ownerType"] == "PRODUCT"


def test_s97_preview_strips_whitespace_in_normalized_payload():
    out_entry = _s97_entry(owner_id=f"  {_S97_PRODUCT_GID}  ", namespace="  custom  ")
    tools, fc = _build([])
    out = tools["set_product_metafields"](metafields=[out_entry], confirm=False)
    assert fc.calls == []
    tail = _parse_tail(out)
    assert tail["metafields"][0]["ownerId"] == _S97_PRODUCT_GID
    assert tail["metafields"][0]["namespace"] == "custom"


# --- Mutation happy path -----------------------------------------------------


def test_s97_mutation_product_owner_path_shape():
    """`metafieldsSet` is invoked with a list missing the internal ownerType key."""
    tools, fc = _build(
        [
            _s97_mutation_response(
                metafields=[
                    {
                        "id": _S97_METAFIELD_GID,
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
    out = tools["set_product_metafields"](metafields=[_s97_entry()], confirm=True)
    assert "CONFIRMED" in out
    assert len(fc.calls) == 1
    _, vars_sent = fc.calls[0]
    assert list(vars_sent["metafields"][0].keys()) == [
        "ownerId",
        "namespace",
        "key",
        "value",
        "type",
    ]
    # Critically: ownerType is NOT sent (Shopify infers it from ownerId).
    assert "ownerType" not in vars_sent["metafields"][0]


def test_s97_mutation_variant_owner_happy_path():
    tools, fc = _build(
        [
            _s97_mutation_response(
                metafields=[
                    {
                        "id": _S97_METAFIELD_GID,
                        "namespace": "custom",
                        "key": "size_chart",
                        "value": "M",
                        "type": "single_line_text_field",
                        "ownerType": "PRODUCT_VARIANT",
                    }
                ]
            )
        ]
    )
    entry = _s97_entry(
        owner_id=_S97_VARIANT_GID,
        key="size_chart",
        value="M",
        mtype="single_line_text_field",
    )
    out = tools["set_product_metafields"](metafields=[entry], confirm=True)
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["metafields"][0]["ownerType"] == "PRODUCT_VARIANT"


def test_s97_mutation_mixed_owner_types_one_call():
    product_entry = _s97_entry()
    variant_entry = _s97_entry(
        owner_id=_S97_VARIANT_GID,
        key="size",
        value="M",
        mtype="single_line_text_field",
    )
    tools, fc = _build(
        [
            _s97_mutation_response(
                metafields=[
                    {
                        "id": _S97_METAFIELD_GID,
                        "namespace": "custom",
                        "key": "fabric_weight_oz",
                        "value": "14",
                        "type": "number_integer",
                        "ownerType": "PRODUCT",
                    },
                    {
                        "id": _S97_METAFIELD_GID_2,
                        "namespace": "custom",
                        "key": "size",
                        "value": "M",
                        "type": "single_line_text_field",
                        "ownerType": "PRODUCT_VARIANT",
                    },
                ]
            )
        ]
    )
    out = tools["set_product_metafields"](metafields=[product_entry, variant_entry], confirm=True)
    assert len(fc.calls) == 1
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert {m["ownerType"] for m in tail["metafields"]} == {"PRODUCT", "PRODUCT_VARIANT"}


def test_s97_success_json_tail_shape():
    """Pin the per-spec key order for each metafield entry in the success tail."""
    tools, fc = _build(
        [
            _s97_mutation_response(
                metafields=[
                    {
                        "id": _S97_METAFIELD_GID,
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
    out = tools["set_product_metafields"](metafields=[_s97_entry()], confirm=True)
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["preview"] is False
    assert tail["errors"] == []
    assert list(tail["metafields"][0].keys()) == [
        "id",
        "namespace",
        "key",
        "value",
        "type",
        "ownerType",
    ]


def test_s97_idempotent_rerun_succeeds_twice():
    # `metafieldsSet` is idempotent at the Shopify level — re-running with
    # identical inputs returns the same metafield IDs/values. AC #9.
    response = _s97_mutation_response(
        metafields=[
            {
                "id": _S97_METAFIELD_GID,
                "namespace": "custom",
                "key": "fabric_weight_oz",
                "value": "14",
                "type": "number_integer",
                "ownerType": "PRODUCT",
            }
        ]
    )
    tools, fc = _build([response, response])
    out1 = tools["set_product_metafields"](metafields=[_s97_entry()], confirm=True)
    out2 = tools["set_product_metafields"](metafields=[_s97_entry()], confirm=True)
    assert _parse_tail(out1)["ok"] is True
    assert _parse_tail(out2)["ok"] is True
    # Two mutation calls, identical variables — no client-side dedupe.
    assert len(fc.calls) == 2
    assert fc.calls[0][1] == fc.calls[1][1]


# --- userError pass-through & per-entry indexing ----------------------------


def test_s97_user_errors_indexed_by_entry():
    # Shopify returns `field: ["metafields", "1", "value"]` for entry 1.
    # The tool must surface this both verbatim and bucketed under
    # errorsByIndex["1"].
    raw_errors = [
        {
            "field": ["metafields", "1", "value"],
            "message": "Value does not match definition.",
            "code": "INVALID_VALUE",
        }
    ]
    tools, fc = _build([_s97_mutation_response(user_errors=raw_errors)])
    out = tools["set_product_metafields"](
        metafields=[_s97_entry(), _s97_entry(key="other")], confirm=True
    )
    assert "metafields.1.value" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["errors"] == raw_errors  # verbatim
    assert "1" in tail["errorsByIndex"]
    assert tail["errorsByIndex"]["1"][0]["message"] == "Value does not match definition."


def test_s97_user_errors_without_index_bucket_under_underscore():
    # Errors with no parseable index (e.g., top-level errors) land under "_".
    raw_errors = [{"field": ["metafields"], "message": "Generic failure", "code": None}]
    tools, fc = _build([_s97_mutation_response(user_errors=raw_errors)])
    out = tools["set_product_metafields"](metafields=[_s97_entry()], confirm=True)
    tail = _parse_tail(out)
    assert "_" in tail["errorsByIndex"]


def test_s97_user_errors_with_empty_field_path_handled():
    # Errors with empty / no `field` list still surface in head and tail.
    raw_errors = [{"field": [], "message": "Mystery error", "code": None}]
    tools, fc = _build([_s97_mutation_response(user_errors=raw_errors)])
    out = tools["set_product_metafields"](metafields=[_s97_entry()], confirm=True)
    assert "(no field)" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False


# --- ACCESS_DENIED + remediation --------------------------------------------


def test_s97_access_denied_emits_remediation_block():
    raw_errors = [
        {
            "field": ["metafields", "0"],
            "message": "Access denied for set_metafield action.",
            "code": "ACCESS_DENIED",
        }
    ]
    tools, fc = _build([_s97_mutation_response(user_errors=raw_errors)])
    out = tools["set_product_metafields"](metafields=[_s97_entry()], confirm=True)
    assert "ACCESS_DENIED" in out
    assert "write_metafields" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert "remediation" in tail
    assert "write_metafields" in tail["remediation"]
    # GRANTED_SCOPES_HINT context is included verbatim.
    assert "write_products" in tail["remediation"]


def test_s97_access_denied_with_mixed_user_errors_routes_to_remediation_branch():
    # If ANY userError carries code=ACCESS_DENIED, the remediation branch wins
    # (per AC #10 — the scope block is the dominant signal).
    raw_errors = [
        {
            "field": ["metafields", "1", "value"],
            "message": "Bad value",
            "code": "INVALID_VALUE",
        },
        {
            "field": ["metafields", "0"],
            "message": "Access denied",
            "code": "ACCESS_DENIED",
        },
    ]
    tools, fc = _build([_s97_mutation_response(user_errors=raw_errors)])
    out = tools["set_product_metafields"](
        metafields=[_s97_entry(), _s97_entry(key="other")], confirm=True
    )
    tail = _parse_tail(out)
    assert "remediation" in tail
    # All raw errors are still surfaced.
    assert tail["errors"] == raw_errors


# --- Network exception path --------------------------------------------------


def test_s97_network_exception_returns_structured_error():
    tools, fc = _build([RuntimeError("boom")])
    out = tools["set_product_metafields"](metafields=[_s97_entry()], confirm=True)
    assert "Error calling metafieldsSet" in out
    assert "RuntimeError" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["metafields"] == []
    assert "boom" in tail["errors"][0]["message"]


# --- Helper-level pins (coverage for branches not exercised via the tool) ---


def test_s97_parse_owner_gid_accepts_product_and_variant():
    assert catalog_hygiene._parse_owner_gid(_S97_PRODUCT_GID) == ("PRODUCT", None)
    assert catalog_hygiene._parse_owner_gid(_S97_VARIANT_GID) == (
        "PRODUCT_VARIANT",
        None,
    )


def test_s97_validate_metafield_value_known_types():
    # Happy paths for each shape-checked type.
    assert catalog_hygiene._validate_metafield_value("14", "number_integer") is None
    assert catalog_hygiene._validate_metafield_value("-14", "number_integer") is None
    assert catalog_hygiene._validate_metafield_value("14.5", "number_decimal") is None
    assert catalog_hygiene._validate_metafield_value("14", "number_decimal") is None
    assert catalog_hygiene._validate_metafield_value("true", "boolean") is None
    assert catalog_hygiene._validate_metafield_value("false", "boolean") is None
    assert catalog_hygiene._validate_metafield_value('{"a": 1}', "json") is None
    assert catalog_hygiene._validate_metafield_value('["a"]', "list.single_line_text_field") is None
    # Unknown type passes through (returns None — no shape check).
    assert catalog_hygiene._validate_metafield_value("anything", "weight") is None
    # Free-text supported types also have no shape check.
    assert catalog_hygiene._validate_metafield_value("anything", "single_line_text_field") is None


def test_s97_err_payload_default_key_is_variants():
    # Backwards-compat with Story 9.6.
    assert catalog_hygiene._err_payload("oops") == {
        "ok": False,
        "variants": [],
        "errors": [{"message": "oops"}],
    }


def test_s97_err_payload_custom_key():
    assert catalog_hygiene._err_payload("oops", key="metafields") == {
        "ok": False,
        "metafields": [],
        "errors": [{"message": "oops"}],
    }


# =============================================================================
# Story 9.5 — update_product_options
# =============================================================================

from tools.catalog_hygiene import (  # noqa: E402
    GET_PRODUCT_OPTIONS,
    GET_PRODUCT_OPTIONS_BY_HANDLE,
    OPTION_NAME_MAX_LEN,
    UPDATE_PRODUCT_OPTION,
)

_OPT_GID = "gid://shopify/ProductOption/300"
_OV_M = "gid://shopify/ProductOptionValue/4001"
_OV_L = "gid://shopify/ProductOptionValue/4002"


def _options_read_response(
    *,
    product_gid: str = "gid://shopify/Product/100",
    title: str = "Test Product",
    option_id: str = _OPT_GID,
    option_name: str = "Size",
    values: list[dict[str, str]] | None = None,
    variants: list[dict[str, Any]] | None = None,
    by_handle: bool = False,
) -> dict:
    """One-read fixture mirroring GET_PRODUCT_OPTIONS / _BY_HANDLE.

    `by_handle=True` wraps the same product under `productByHandle` so a single
    helper covers both resolver paths.
    """
    node: dict[str, Any] = {
        "id": product_gid,
        "title": title,
        "options": [
            {
                "id": option_id,
                "name": option_name,
                "optionValues": values
                or [
                    {"id": _OV_M, "name": "M-CRM"},
                    {"id": _OV_L, "name": "L-CRM"},
                ],
            }
        ],
        "variants": {
            "nodes": variants
            or [
                {
                    "id": "gid://shopify/ProductVariant/501",
                    "title": "M-CRM / Cream",
                    "selectedOptions": [{"name": "Size", "value": "M-CRM"}],
                }
            ],
        },
    }
    return {"productByHandle": node} if by_handle else {"product": node}


def _options_mutation_ok(
    *,
    product_gid: str = "gid://shopify/Product/100",
    option_id: str = _OPT_GID,
    option_name: str = "Size",
    values: list[dict[str, str]] | None = None,
    variants: list[dict[str, Any]] | None = None,
) -> dict:
    return {
        "productOptionUpdate": {
            "product": {
                "id": product_gid,
                "options": [
                    {
                        "id": option_id,
                        "name": option_name,
                        "optionValues": values
                        or [
                            {"id": _OV_M, "name": "Medium"},
                            {"id": _OV_L, "name": "L-CRM"},
                        ],
                    }
                ],
                "variants": {
                    "nodes": variants
                    or [
                        {
                            "id": "gid://shopify/ProductVariant/501",
                            "title": "Medium / Cream",
                            "selectedOptions": [{"name": "Size", "value": "Medium"}],
                        }
                    ],
                },
            },
            "userErrors": [],
        }
    }


# Validation rejects — no network call -----------------------------------------


@pytest.mark.parametrize(
    "option,values,strategy,fragment",
    [
        # option arg shape
        (None, None, "LEAVE_AS_IS", "option must be an object"),
        ("not-a-dict", None, "LEAVE_AS_IS", "option must be an object"),
        ({}, None, "LEAVE_AS_IS", "option.id must be a non-empty string"),
        ({"id": ""}, None, "LEAVE_AS_IS", "option.id must be a non-empty string"),
        ({"id": "   "}, None, "LEAVE_AS_IS", "option.id must be a non-empty string"),
        ({"id": 123}, None, "LEAVE_AS_IS", "option.id must be a non-empty string"),
        ({"id": "gid://shopify/Product/1"}, None, "LEAVE_AS_IS", "must be a ProductOption GID"),
        (
            {"id": "gid://shopify/ProductOption/"},
            None,
            "LEAVE_AS_IS",
            "empty GID body",
        ),
        ({"id": _OPT_GID, "name": ""}, None, "LEAVE_AS_IS", "option.name, when supplied"),
        ({"id": _OPT_GID, "name": "   "}, None, "LEAVE_AS_IS", "option.name, when supplied"),
        (
            {"id": _OPT_GID, "name": "X" * (OPTION_NAME_MAX_LEN + 1)},
            None,
            "LEAVE_AS_IS",
            f"exceeds {OPTION_NAME_MAX_LEN}-char limit",
        ),
        # option_values_to_update arg shape
        ({"id": _OPT_GID}, "not-a-list", "LEAVE_AS_IS", "must be a list"),
        ({"id": _OPT_GID}, ["not-a-dict"], "LEAVE_AS_IS", "must be an object"),
        ({"id": _OPT_GID}, [{}], "LEAVE_AS_IS", "option_values_to_update[0].id"),
        ({"id": _OPT_GID}, [{"id": ""}], "LEAVE_AS_IS", "option_values_to_update[0].id"),
        (
            {"id": _OPT_GID},
            [{"id": "gid://shopify/Product/1"}],
            "LEAVE_AS_IS",
            "ProductOptionValue GID",
        ),
        (
            {"id": _OPT_GID},
            [{"id": "gid://shopify/ProductOptionValue/"}],
            "LEAVE_AS_IS",
            "empty GID body",
        ),
        (
            {"id": _OPT_GID},
            [{"id": _OV_M, "name": "Medium-1"}, {"id": _OV_M, "name": "Medium-2"}],
            "LEAVE_AS_IS",
            "is a duplicate",
        ),
        (
            {"id": _OPT_GID},
            [{"id": _OV_M, "name": ""}],
            "LEAVE_AS_IS",
            "option_values_to_update[0].name",
        ),
        (
            {"id": _OPT_GID},
            [{"id": _OV_M, "name": "X" * (OPTION_NAME_MAX_LEN + 1)}],
            "LEAVE_AS_IS",
            f"exceeds {OPTION_NAME_MAX_LEN}-char limit",
        ),
        # variant_strategy
        ({"id": _OPT_GID}, None, "INVALID", "variant_strategy must be one of"),
        ({"id": _OPT_GID}, None, 123, "variant_strategy must be one of"),
    ],
)
def test_s95_validation_rejects_no_network(option, values, strategy, fragment):
    tools, fc = _build([])
    out = tools["update_product_options"](
        product_id="100",
        option=option,
        option_values_to_update=values,
        variant_strategy=strategy,
    )
    assert out.startswith("Error:")
    assert fragment in out
    assert fc.calls == []
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["preview"] is False
    assert tail["errors"][0]["stage"] == "validation"


# Product resolver paths -------------------------------------------------------


def test_s95_resolver_empty_product_id_raises_caught():
    tools, fc = _build([])
    out = tools["update_product_options"](
        product_id="",
        option={"id": _OPT_GID, "name": "Size"},
    )
    assert out.startswith("Error resolving product_id")
    # ValueError is caught before any network call.
    assert fc.calls == []
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["errors"][0]["stage"] == "product-resolve"


def test_s95_resolver_empty_gid_body_raises_caught():
    tools, fc = _build([])
    out = tools["update_product_options"](
        product_id="gid://shopify/Product/",
        option={"id": _OPT_GID, "name": "Size"},
    )
    assert "Empty product GID body" in out
    assert fc.calls == []


def test_s95_no_product_found_numeric():
    tools, fc = _build([{"product": None}])
    out = tools["update_product_options"](
        product_id="404",
        option={"id": _OPT_GID, "name": "Size"},
    )
    assert "Error: no product found for '404'" in out
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_OPTIONS
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["errors"][0]["stage"] == "product-resolve"


def test_s95_no_product_found_handle():
    tools, fc = _build([{"productByHandle": None}])
    out = tools["update_product_options"](
        product_id="missing-handle",
        option={"id": _OPT_GID, "name": "Size"},
    )
    assert "Error: no product found for 'missing-handle'" in out
    assert fc.calls[0][0] == GET_PRODUCT_OPTIONS_BY_HANDLE


def test_s95_resolves_gid_path():
    # Read returns current state — empty-delta path short-circuits (no mutation).
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="gid://shopify/Product/100",
        option={"id": _OPT_GID},  # no name, no values → no-op short-circuit
    )
    assert "no-op, no changes requested" in out
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_OPTIONS
    assert fc.calls[0][1] == {"id": "gid://shopify/Product/100"}


def test_s95_resolves_handle_path():
    tools, fc = _build([_options_read_response(by_handle=True)])
    out = tools["update_product_options"](
        product_id="vanish-crewneck",
        option={"id": _OPT_GID},
    )
    assert "no-op, no changes requested" in out
    assert fc.calls[0][0] == GET_PRODUCT_OPTIONS_BY_HANDLE
    assert fc.calls[0][1] == {"handle": "vanish-crewneck"}


# Option / option-value validation against the read ---------------------------


def test_s95_option_not_on_product():
    other_opt = "gid://shopify/ProductOption/999"
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="100",
        option={"id": other_opt, "name": "Color"},
    )
    assert "is not on product '100'" in out
    assert len(fc.calls) == 1  # read only — no mutation
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["errors"][0]["stage"] == "option-validation"
    # Product snapshot still rendered so the caller can audit pre-state.
    assert tail["product"]["id"] == "gid://shopify/Product/100"


def test_s95_option_value_not_on_option():
    stranger_value = "gid://shopify/ProductOptionValue/9999"
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID},
        option_values_to_update=[{"id": stranger_value, "name": "Medium"}],
    )
    assert "not on option" in out
    assert stranger_value in out
    assert len(fc.calls) == 1
    tail = _parse_tail(out)
    assert tail["errors"][0]["stage"] == "option-value-validation"


# Short-circuit paths ----------------------------------------------------------


def test_s95_empty_delta_short_circuit():
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID},
        option_values_to_update=None,
    )
    assert "no-op, no changes requested" in out
    assert len(fc.calls) == 1  # read only
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["preview"] is False
    assert tail["product"]["options"][0]["name"] == "Size"


def test_s95_empty_delta_short_circuit_explicit_empty_list():
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID},
        option_values_to_update=[],
    )
    assert "no-op, no changes requested" in out
    assert len(fc.calls) == 1


def test_s95_idempotent_no_op_name_only():
    # Caller requests rename to the same current name → no-op (no mutation).
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Size"},  # already "Size"
        confirm=True,
    )
    assert "no-op, already set" in out
    assert len(fc.calls) == 1  # read only


def test_s95_idempotent_no_op_values_match():
    # Every value rename matches the current value name → no-op (no mutation).
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Size"},
        option_values_to_update=[
            {"id": _OV_M, "name": "M-CRM"},  # current
            {"id": _OV_L, "name": "L-CRM"},  # current
        ],
        confirm=True,
    )
    assert "no-op, already set" in out
    assert len(fc.calls) == 1  # read only


# Preview path -----------------------------------------------------------------


def test_s95_preview_no_mutation_with_name_and_value_diffs():
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Sizing"},
        option_values_to_update=[{"id": _OV_M, "name": "Medium"}],
    )
    assert out.startswith("PREVIEW —")
    assert "Reply with confirm=True" in out
    # Diff lines render old → new for both option name and the renamed value.
    assert "'Size' → 'Sizing'" in out
    assert "'M-CRM' → 'Medium'" in out
    assert "Strategy      : LEAVE_AS_IS" in out
    assert len(fc.calls) == 1  # read only
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["preview"] is True
    # Snapshot is pre-state — option still "Size", value still "M-CRM".
    assert tail["product"]["options"][0]["name"] == "Size"
    assert tail["product"]["options"][0]["optionValues"][0]["name"] == "M-CRM"


def test_s95_preview_name_only_change_omits_value_diffs():
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Sizing"},
        option_values_to_update=None,
    )
    assert "'Size' → 'Sizing'" in out
    # No value renames requested → no value-diff lines.
    assert "M-CRM" not in out.split("```json")[0]
    assert len(fc.calls) == 1


def test_s95_preview_values_only_change_omits_name_diff():
    tools, fc = _build([_options_read_response()])
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID},  # no name change
        option_values_to_update=[{"id": _OV_M, "name": "Medium"}],
    )
    head = out.split("```json")[0]
    assert "Option name" not in head
    assert "'M-CRM' → 'Medium'" in out
    assert len(fc.calls) == 1


# Confirm path — mutation called ----------------------------------------------


def test_s95_confirm_executes_mutation_full_payload():
    tools, fc = _build(
        [
            _options_read_response(),
            _options_mutation_ok(
                option_name="Sizing",
                values=[
                    {"id": _OV_M, "name": "Medium"},
                    {"id": _OV_L, "name": "L-CRM"},
                ],
            ),
        ]
    )
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Sizing"},
        option_values_to_update=[{"id": _OV_M, "name": "Medium"}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —")
    # Mutation called with the exact shape: name in option_input, values list,
    # explicit variantStrategy default.
    assert fc.calls[1][0] == UPDATE_PRODUCT_OPTION
    mutation_vars = fc.calls[1][1]
    assert mutation_vars == {
        "productId": "gid://shopify/Product/100",
        "option": {"id": _OPT_GID, "name": "Sizing"},
        "optionValuesToUpdate": [{"id": _OV_M, "name": "Medium"}],
        "variantStrategy": "LEAVE_AS_IS",
    }
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["preview"] is False
    # Post-state snapshot reflects Shopify's echoed product.
    assert tail["product"]["options"][0]["name"] == "Sizing"
    assert tail["product"]["options"][0]["optionValues"][0]["name"] == "Medium"
    assert tail["product"]["variants"][0]["selectedOptions"][0]["value"] == "Medium"


def test_s95_confirm_name_only_omits_name_from_option_input_when_no_name():
    # No `name` change requested → mutation `option` input should NOT include
    # a `name` field. Renames a value to exercise that branch.
    tools, fc = _build(
        [
            _options_read_response(),
            _options_mutation_ok(
                values=[{"id": _OV_M, "name": "Medium"}, {"id": _OV_L, "name": "L-CRM"}]
            ),
        ]
    )
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID},
        option_values_to_update=[{"id": _OV_M, "name": "Medium"}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —")
    mutation_vars = fc.calls[1][1]
    assert mutation_vars["option"] == {"id": _OPT_GID}
    assert "name" not in mutation_vars["option"]


def test_s95_confirm_omits_name_when_matches_current_alongside_value_renames():
    # Code-review Suggestion 2: when the caller passes option.name matching the
    # current name AND there are real value renames, the mutation input should
    # OMIT the redundant `name` field (no-op slot save). The call still ships
    # because values_no_op is False — only the `name` slot is stripped.
    tools, fc = _build(
        [
            _options_read_response(),  # current option name is "Size"
            _options_mutation_ok(
                values=[{"id": _OV_M, "name": "Medium"}, {"id": _OV_L, "name": "L-CRM"}]
            ),
        ]
    )
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Size"},  # redundant — matches current
        option_values_to_update=[{"id": _OV_M, "name": "Medium"}],
        confirm=True,
    )
    assert out.startswith("CONFIRMED —")
    # Mutation still ran (one value rename is real)…
    assert fc.calls[1][0] == UPDATE_PRODUCT_OPTION
    # …but `name` was stripped from the option input.
    mutation_vars = fc.calls[1][1]
    assert mutation_vars["option"] == {"id": _OPT_GID}
    assert "name" not in mutation_vars["option"]
    # Diff body also shouldn't surface a redundant "Option name" line.
    head = out.split("```json")[0]
    assert "Option name" not in head
    # Value rename does surface in the diff.
    assert "'M-CRM' → 'Medium'" in out


def test_s95_confirm_variant_strategy_manage_passed_through():
    tools, fc = _build(
        [
            _options_read_response(),
            _options_mutation_ok(option_name="Sizing"),
        ]
    )
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Sizing"},
        variant_strategy="MANAGE",
        confirm=True,
    )
    assert out.startswith("CONFIRMED —")
    assert "Strategy      : MANAGE" in out
    assert fc.calls[1][1]["variantStrategy"] == "MANAGE"


# Confirm path — error mapping ------------------------------------------------


def test_s95_confirm_user_errors_preserves_code():
    tools, fc = _build(
        [
            _options_read_response(),
            {
                "productOptionUpdate": {
                    "product": None,
                    "userErrors": [
                        {
                            "field": ["optionValuesToUpdate", "0", "name"],
                            "message": "Option value name already exists.",
                            "code": "DUPLICATE_OPTION_VALUE_NAME",
                        }
                    ],
                }
            },
        ]
    )
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID},
        option_values_to_update=[{"id": _OV_M, "name": "L-CRM"}],
        confirm=True,
    )
    assert out.startswith("Error: productOptionUpdate userErrors")
    assert "DUPLICATE_OPTION_VALUE_NAME" in out  # code surfaced in the head
    assert "optionValuesToUpdate.0.name" in out  # dotted field path
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["errors"][0]["code"] == "DUPLICATE_OPTION_VALUE_NAME"


def test_s95_confirm_user_errors_no_code():
    # Defensive: an error without a `code` shouldn't emit "[] ".
    tools, fc = _build(
        [
            _options_read_response(),
            {
                "productOptionUpdate": {
                    "product": None,
                    "userErrors": [
                        {"field": [], "message": "Something went wrong.", "code": None},
                    ],
                }
            },
        ]
    )
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Sizing"},
        confirm=True,
    )
    assert "Error: productOptionUpdate userErrors" in out
    assert "(no field): Something went wrong." in out
    # No square-bracket code marker when code is None/missing.
    assert "[None]" not in out


def test_s95_confirm_mutation_raises_caught():
    tools, fc = _build(
        [
            _options_read_response(),
            RuntimeError("HTTP 500"),
        ]
    )
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Sizing"},
        confirm=True,
    )
    assert "Error calling productOptionUpdate (RuntimeError)" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["errors"][0]["stage"] == "option-update"


def test_s95_confirm_success_with_null_returned_product():
    # Defensive: Shopify omits/nulls the `product` slot but userErrors is empty.
    # The success path should still produce a well-shaped (empty) snapshot.
    tools, fc = _build(
        [
            _options_read_response(),
            {"productOptionUpdate": {"product": None, "userErrors": []}},
        ]
    )
    out = tools["update_product_options"](
        product_id="100",
        option={"id": _OPT_GID, "name": "Sizing"},
        confirm=True,
    )
    assert out.startswith("CONFIRMED —")
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["product"] == {"id": "", "options": [], "variants": []}


# Helper sanity ---------------------------------------------------------------


def test_s95_shape_options_snapshot_handles_none():
    assert catalog_hygiene._shape_options_snapshot(None) == {
        "id": "",
        "options": [],
        "variants": [],
    }


def test_s95_shape_options_snapshot_handles_empty_dict():
    assert catalog_hygiene._shape_options_snapshot({}) == {
        "id": "",
        "options": [],
        "variants": [],
    }


# =============================================================================
# Story 9.10 — delete_product_metafields
# =============================================================================
#
# AC coverage map (see context/EPIC_9_addendum_metafield_reads.md §Story 9.10):
#   AC #1  metafields[] required, ≤ 25      → test_s910_validation_rejects, _25_cap
#   AC #2  exactly one of metafieldId|triple → test_s910_mixed_addressing_*, _neither_*
#   AC #3  metafieldId direct / triple resolves → test_s910_metafieldId_path_*, _triple_path_*
#   AC #4  fail-fast on first invalid entry → test_s910_fail_fast_*
#   AC #5  dry-run resolves but no mutation → test_s910_dryrun_resolves_but_no_mutation
#   AC #6  userErrors keyed by entry index  → test_s910_errors_keyed_by_entry_index
#   AC #7  NOT_FOUND treated as idempotent  → test_s910_idempotent_not_found_*
#   AC #8  return shape                     → test_s910_metafieldId_path_*, _success_*

from tools.catalog_hygiene import (  # noqa: E402
    METAFIELDS_DELETE_MAX,
    METAFIELDS_DELETE_MUTATION,
)

_S910_PRODUCT_GID = "gid://shopify/Product/777"
_S910_VARIANT_GID = "gid://shopify/ProductVariant/8001"
_S910_METAFIELD_GID = "gid://shopify/Metafield/42"
_S910_METAFIELD_GID_2 = "gid://shopify/Metafield/43"


def _s910_batch_resolve(*aliases: dict) -> dict:
    """Build a batched resolve response keyed by `e0`, `e1`, ...

    Each positional arg is the per-alias response shape (see
    `_build_batch_resolve_query`'s docstring) — pass `None` for owner-not-found
    in triple mode, or build an explicit dict for the matching mode's payload.
    """
    return {f"e{i}": a for i, a in enumerate(aliases)}


def _s910_gid_alias(
    *,
    metafield_id: str = _S910_METAFIELD_GID,
    namespace: str = "custom",
    key: str = "fabric_weight_oz",
    owner_type: str = "PRODUCT",
    owner_gid: str = _S910_PRODUCT_GID,
) -> dict:
    """Per-alias `e<i>` shape for a metafieldId-mode lookup (happy path)."""
    return {
        "id": metafield_id,
        "namespace": namespace,
        "key": key,
        "ownerType": owner_type,
        "owner": {"id": owner_gid},
    }


def _s910_triple_alias(
    *,
    metafield_id: str | None = _S910_METAFIELD_GID,
    namespace: str = "custom",
    key: str = "fabric_weight_oz",
    owner_type: str = "PRODUCT",
) -> dict:
    """Per-alias `e<i>` shape for a triple-mode lookup.

    Passing `metafield_id=None` simulates "owner exists but metafield does not"
    — the idempotent success-with-note branch.
    """
    metafield_block: dict | None = (
        None
        if metafield_id is None
        else {
            "id": metafield_id,
            "namespace": namespace,
            "key": key,
            "ownerType": owner_type,
        }
    )
    return {"metafield": metafield_block}


def _s910_delete_response(deleted=None, user_errors=None):
    return {
        "metafieldsDelete": {
            "deletedMetafields": deleted or [],
            "userErrors": user_errors or [],
        }
    }


# --- Validation rejects (no network) -----------------------------------------


@pytest.mark.parametrize(
    "metafields,fragment",
    [
        (None, "metafields must be a non-empty list"),
        ([], "metafields must be a non-empty list"),
        ("not-a-list", "metafields must be a non-empty list"),
        (["not-a-dict"], "metafields[0] must be an object"),
        # Neither addressing — empty entry.
        ([{}], "needs either `metafieldId`"),
        # Both addressing modes in one entry.
        (
            [
                {
                    "metafieldId": _S910_METAFIELD_GID,
                    "ownerId": _S910_PRODUCT_GID,
                    "namespace": "custom",
                    "key": "k",
                }
            ],
            "has both `metafieldId` and the",
        ),
        # Malformed metafield GID.
        ([{"metafieldId": "not-a-gid"}], "must be a Metafield GID"),
        ([{"metafieldId": "gid://shopify/Product/123"}], "must be a Metafield GID"),
        ([{"metafieldId": "gid://shopify/Metafield/"}], "has empty GID body"),
        ([{"metafieldId": ""}], "must be a non-empty string"),
        # Triple missing pieces.
        (
            [{"ownerId": _S910_PRODUCT_GID, "namespace": "", "key": "k"}],
            "namespace must be a non-empty string",
        ),
        (
            [{"ownerId": _S910_PRODUCT_GID, "namespace": "custom", "key": "   "}],
            "key must be a non-empty string",
        ),
    ],
)
def test_s910_validation_rejects(metafields, fragment):
    tools, fc = _build([])
    out = tools["delete_product_metafields"](metafields=metafields)
    assert out.startswith("Error:")
    assert fragment in out
    assert fc.calls == []  # No network call before validation passes.
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["deleted"] == []


def test_s910_25_cap_enforced():
    tools, fc = _build([])
    over_cap = [{"metafieldId": _S910_METAFIELD_GID}] * (METAFIELDS_DELETE_MAX + 1)
    out = tools["delete_product_metafields"](metafields=over_cap)
    assert "exceeds the 25-entry" in out
    assert fc.calls == []


def test_s910_fail_fast_on_first_invalid_entry():
    # Mixed batch: entry 0 is valid, entry 1 is the bad one — call halts
    # at entry 1 and emits no network calls (AC #4).
    tools, fc = _build([])
    out = tools["delete_product_metafields"](
        metafields=[
            {"metafieldId": _S910_METAFIELD_GID},
            {"metafieldId": "not-a-gid"},
        ]
    )
    assert "metafields[1]" in out
    assert fc.calls == []


# --- metafieldId path --------------------------------------------------------


def test_s910_metafieldId_path_deletes_one():
    tools, fc = _build(
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
    out = tools["delete_product_metafields"](
        metafields=[{"metafieldId": _S910_METAFIELD_GID}], confirm=True
    )
    assert "CONFIRMED —" in out
    # 1 batched resolution + 1 mutation = 2 calls.
    assert len(fc.calls) == 2
    assert "BatchResolveMetafields" in fc.calls[0][0]
    assert fc.calls[0][1] == {"id0": _S910_METAFIELD_GID}
    assert fc.calls[1][0] == METAFIELDS_DELETE_MUTATION
    # Mutation input is the triple, not the GID — `metafieldsDelete` takes
    # MetafieldIdentifierInput, not metafield GIDs.
    assert fc.calls[1][1]["metafields"] == [
        {"ownerId": _S910_PRODUCT_GID, "namespace": "custom", "key": "fabric_weight_oz"}
    ]
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["preview"] is False
    assert tail["deleted"] == [
        {
            "id": _S910_METAFIELD_GID,
            "namespace": "custom",
            "key": "fabric_weight_oz",
            "ownerType": "PRODUCT",
            "ownerId": _S910_PRODUCT_GID,
        }
    ]


def test_s910_variant_owner_dispatches_to_node_query():
    # Variant owner — alias lookup returns the metafield with ownerType
    # PRODUCT_VARIANT. Mutation input still uses the variant GID as ownerId.
    tools, fc = _build(
        [
            _s910_batch_resolve(
                _s910_gid_alias(
                    owner_type="PRODUCT_VARIANT",
                    owner_gid=_S910_VARIANT_GID,
                )
            ),
            _s910_delete_response(
                deleted=[
                    {
                        "ownerId": _S910_VARIANT_GID,
                        "namespace": "custom",
                        "key": "fabric_weight_oz",
                    }
                ]
            ),
        ]
    )
    out = tools["delete_product_metafields"](
        metafields=[{"metafieldId": _S910_METAFIELD_GID}], confirm=True
    )
    assert fc.calls[1][1]["metafields"][0]["ownerId"] == _S910_VARIANT_GID
    tail = _parse_tail(out)
    assert tail["deleted"][0]["ownerType"] == "PRODUCT_VARIANT"


# --- triple path -------------------------------------------------------------


def test_s910_triple_path_resolves_then_deletes():
    tools, fc = _build(
        [
            _s910_batch_resolve(_s910_triple_alias()),
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
    out = tools["delete_product_metafields"](
        metafields=[
            {
                "ownerId": _S910_PRODUCT_GID,
                "namespace": "custom",
                "key": "fabric_weight_oz",
            }
        ],
        confirm=True,
    )
    assert "CONFIRMED —" in out
    assert len(fc.calls) == 2
    assert "BatchResolveMetafields" in fc.calls[0][0]
    assert fc.calls[0][1] == {
        "ownerId0": _S910_PRODUCT_GID,
        "ns0": "custom",
        "k0": "fabric_weight_oz",
    }
    assert fc.calls[1][0] == METAFIELDS_DELETE_MUTATION
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["deleted"][0]["id"] == _S910_METAFIELD_GID


# --- Dry-run -----------------------------------------------------------------


def test_s910_dryrun_resolves_but_no_mutation():
    # Triple input → 1 batched resolve call. confirm=False short-circuits
    # the mutation.
    tools, fc = _build([_s910_batch_resolve(_s910_triple_alias())])
    out = tools["delete_product_metafields"](
        metafields=[
            {
                "ownerId": _S910_PRODUCT_GID,
                "namespace": "custom",
                "key": "fabric_weight_oz",
            }
        ],
        confirm=False,
    )
    assert "PREVIEW —" in out
    assert "To apply, call again with confirm=True" in out
    assert len(fc.calls) == 1
    assert "BatchResolveMetafields" in fc.calls[0][0]
    tail = _parse_tail(out)
    assert tail["preview"] is True
    assert tail["ok"] is True
    assert tail["deleted"] == [
        {
            "id": _S910_METAFIELD_GID,
            "namespace": "custom",
            "key": "fabric_weight_oz",
            "ownerType": "PRODUCT",
            "ownerId": _S910_PRODUCT_GID,
        }
    ]


# --- Idempotent NOT_FOUND ----------------------------------------------------


def test_s910_idempotent_not_found_via_resolution_is_success_with_note():
    # Triple resolution returns metafield=None — owner exists but metafield
    # doesn't. Tool short-circuits the mutation entirely; no second call.
    tools, fc = _build([_s910_batch_resolve(_s910_triple_alias(metafield_id=None))])
    out = tools["delete_product_metafields"](
        metafields=[
            {
                "ownerId": _S910_PRODUCT_GID,
                "namespace": "custom",
                "key": "fabric_weight_oz",
            }
        ],
        confirm=True,
    )
    assert "CONFIRMED —" in out
    assert len(fc.calls) == 1  # Batched resolution only, no mutation.
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["deleted"][0]["note"] == ("Metafield not found — treated as idempotent success")


def test_s910_idempotent_not_found_from_shopify_user_error_is_success_with_note():
    # Resolution succeeds, mutation returns NOT_FOUND — also idempotent.
    tools, fc = _build(
        [
            _s910_batch_resolve(_s910_gid_alias()),
            _s910_delete_response(
                user_errors=[
                    {
                        "field": ["metafields", "0"],
                        "message": "Metafield not found",
                        "code": "NOT_FOUND",
                    }
                ]
            ),
        ]
    )
    out = tools["delete_product_metafields"](
        metafields=[{"metafieldId": _S910_METAFIELD_GID}], confirm=True
    )
    assert "CONFIRMED —" in out
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["deleted"][0]["note"] == ("Metafield not found — treated as idempotent success")


# --- Per-entry errors keyed by index ----------------------------------------


def test_s910_errors_keyed_by_entry_index():
    # Two-entry batch where entry 1 fails with a non-idempotent error
    # (ACCESS_DENIED). One batched resolve + one mutation = 2 calls.
    tools, fc = _build(
        [
            _s910_batch_resolve(
                _s910_gid_alias(),
                _s910_gid_alias(metafield_id=_S910_METAFIELD_GID_2, key="care_instructions"),
            ),
            _s910_delete_response(
                user_errors=[
                    {
                        "field": ["metafields", "1"],
                        "message": "Access denied",
                        "code": "ACCESS_DENIED",
                    }
                ]
            ),
        ]
    )
    out = tools["delete_product_metafields"](
        metafields=[
            {"metafieldId": _S910_METAFIELD_GID},
            {"metafieldId": _S910_METAFIELD_GID_2},
        ],
        confirm=True,
    )
    assert out.startswith("Error: metafieldsDelete userErrors")
    assert len(fc.calls) == 2  # 1 batched resolve + 1 mutation.
    tail = _parse_tail(out)
    assert tail["ok"] is False
    # Verbatim — including the `code` field per AC #6.
    assert tail["errors"][0]["code"] == "ACCESS_DENIED"
    assert "1" in tail["errorsByIndex"]
    assert tail["errorsByIndex"]["1"][0]["code"] == "ACCESS_DENIED"


# --- Network exception path --------------------------------------------------


def test_s910_resolution_network_exception_returns_structured_error():
    # Batched resolution call raises. Note: the batched call covers the whole
    # input, so the error message doesn't pinpoint a specific entry — that's
    # acceptable for a transport-level failure (the whole batch is the unit
    # of work).
    tools, fc = _build([RuntimeError("boom")])
    out = tools["delete_product_metafields"](
        metafields=[{"metafieldId": _S910_METAFIELD_GID}], confirm=True
    )
    assert "Error resolving metafields" in out
    assert "RuntimeError" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False


# --- Helper-level pins -------------------------------------------------------


def test_s910_parse_metafield_gid_happy_and_sad():
    assert catalog_hygiene._parse_metafield_gid(_S910_METAFIELD_GID) is None
    assert "must be a non-empty string" in catalog_hygiene._parse_metafield_gid(None)
    assert "must be a non-empty string" in catalog_hygiene._parse_metafield_gid("   ")
    assert "must be a Metafield GID" in catalog_hygiene._parse_metafield_gid("12345")
    assert "must be a Metafield GID" in catalog_hygiene._parse_metafield_gid(_S910_PRODUCT_GID)
    assert "empty GID body" in catalog_hygiene._parse_metafield_gid("gid://shopify/Metafield/")


def test_s910_resolve_owner_gid_rejects_numeric_as_ambiguous():
    fc = FakeClient([])
    gid, ot, err = catalog_hygiene._resolve_owner_gid_for_metafield(fc, "12345")
    assert gid is None
    assert ot is None
    assert "ambiguous" in err
    # Confirm no GraphQL call was issued — numeric short-circuits.
    assert fc.calls == []


def test_s910_resolve_owner_gid_accepts_product_handle():
    # Product handle → triggers _resolve_product_gid → productByHandle query.
    fc = FakeClient([{"productByHandle": {"id": _S910_PRODUCT_GID, "title": "Test"}}])
    gid, ot, err = catalog_hygiene._resolve_owner_gid_for_metafield(fc, "test-handle")
    assert gid == _S910_PRODUCT_GID
    assert ot == "PRODUCT"
    assert err is None


def test_s910_resolve_owner_gid_rejects_non_string_and_empty():
    # Two distinct branches of the "not a non-empty string" guard.
    fc = FakeClient([])
    _, _, err_none = catalog_hygiene._resolve_owner_gid_for_metafield(fc, None)
    _, _, err_empty = catalog_hygiene._resolve_owner_gid_for_metafield(fc, "   ")
    assert "non-empty string" in err_none
    assert "non-empty string" in err_empty
    assert fc.calls == []


def test_s910_resolve_owner_gid_rejects_empty_product_and_variant_gid_bodies():
    fc = FakeClient([])
    _, _, err_p = catalog_hygiene._resolve_owner_gid_for_metafield(fc, "gid://shopify/Product/")
    _, _, err_v = catalog_hygiene._resolve_owner_gid_for_metafield(
        fc, "gid://shopify/ProductVariant/"
    )
    assert "empty GID body" in err_p
    assert "empty GID body" in err_v
    assert fc.calls == []


def test_s910_resolve_owner_gid_accepts_variant_gid_short_circuit():
    # Variant GID short-circuits — no GraphQL call.
    fc = FakeClient([])
    gid, ot, err = catalog_hygiene._resolve_owner_gid_for_metafield(fc, _S910_VARIANT_GID)
    assert gid == _S910_VARIANT_GID
    assert ot == "PRODUCT_VARIANT"
    assert err is None
    assert fc.calls == []


def test_s910_metafieldId_path_not_found_is_idempotent_success_with_note():
    # Batched alias returns null for the metafieldId entry → metafield doesn't
    # exist. No mutation issued; per-entry note explains the idempotent behavior.
    tools, fc = _build([_s910_batch_resolve(None)])
    out = tools["delete_product_metafields"](
        metafields=[{"metafieldId": _S910_METAFIELD_GID}], confirm=True
    )
    assert "CONFIRMED —" in out
    assert len(fc.calls) == 1  # Resolution only, no mutation.
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["deleted"][0]["note"] == ("Metafield not found — treated as idempotent success")
    assert tail["deleted"][0]["id"] == _S910_METAFIELD_GID


def test_s910_metafieldId_path_empty_alias_dict_is_idempotent_success_with_note():
    # When the supplied GID points to a non-Metafield node, the `... on
    # Metafield` fragment doesn't match and the alias resolves to {} rather
    # than null. Both shapes route to the idempotent-success branch.
    tools, fc = _build([_s910_batch_resolve({})])
    out = tools["delete_product_metafields"](
        metafields=[{"metafieldId": _S910_METAFIELD_GID}], confirm=True
    )
    assert "CONFIRMED —" in out
    assert len(fc.calls) == 1
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["deleted"][0]["note"] == ("Metafield not found — treated as idempotent success")


def test_s910_triple_path_owner_not_found_returns_hard_error():
    # Triple ownerId resolves but the batched alias returns null →
    # owner GID itself didn't resolve. Tool surfaces this under
    # `Error resolving metafields[N]:` and emits no mutation call.
    tools, fc = _build([_s910_batch_resolve(None)])
    out = tools["delete_product_metafields"](
        metafields=[
            {
                "ownerId": _S910_PRODUCT_GID,
                "namespace": "custom",
                "key": "fabric_weight_oz",
            }
        ],
        confirm=True,
    )
    assert "Error resolving metafields[0]" in out
    assert "not found" in out
    assert len(fc.calls) == 1  # Only batched resolution, no mutation.
    tail = _parse_tail(out)
    assert tail["ok"] is False


def test_s910_handle_resolution_failure_propagates_before_batched_resolve():
    # ownerId is a handle that productByHandle returns null for. Handle
    # resolution is per-entry (it can't be aliased into the metafield batch
    # because the metafield batch depends on its output). The error surfaces
    # without the batched resolve call being issued.
    tools, fc = _build([{"productByHandle": None}])
    out = tools["delete_product_metafields"](
        metafields=[
            {
                "ownerId": "ghost-handle",
                "namespace": "custom",
                "key": "fabric_weight_oz",
            }
        ],
        confirm=True,
    )
    assert "Error resolving metafields[0]" in out
    assert "No product found" in out
    assert len(fc.calls) == 1  # Only the productByHandle call — no batched resolve.
    tail = _parse_tail(out)
    assert tail["ok"] is False


def test_s910_mutation_network_exception_returns_structured_error():
    # Resolution succeeds, mutation network call raises.
    tools, fc = _build([_s910_batch_resolve(_s910_gid_alias()), RuntimeError("kaboom")])
    out = tools["delete_product_metafields"](
        metafields=[{"metafieldId": _S910_METAFIELD_GID}], confirm=True
    )
    assert "Error calling metafieldsDelete" in out
    assert "RuntimeError" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False


# --- Batched query builder ---------------------------------------------------


def test_s910_build_batch_resolve_query_mixed_modes():
    # Mixed gid + triple input → one query with two aliases, each variable
    # uniquely numbered.
    classified = [
        {"idx": 0, "mode": "gid", "gid": _S910_METAFIELD_GID},
        {
            "idx": 1,
            "mode": "triple",
            "ownerId": _S910_PRODUCT_GID,
            "ownerType": "PRODUCT",
            "namespace": "custom",
            "key": "fabric_weight_oz",
        },
    ]
    query, variables = catalog_hygiene._build_batch_resolve_query(classified)
    assert "BatchResolveMetafields" in query
    assert "$id0: ID!" in query
    assert "$ownerId1: ID!" in query
    assert "$ns1: String!" in query
    assert "$k1: String!" in query
    assert "e0: node(id: $id0)" in query
    assert "e1: node(id: $ownerId1)" in query
    assert "... on Metafield" in query  # gid mode fragment
    assert "... on Product" in query  # triple mode fragment
    assert "... on ProductVariant" in query
    assert variables == {
        "id0": _S910_METAFIELD_GID,
        "ownerId1": _S910_PRODUCT_GID,
        "ns1": "custom",
        "k1": "fabric_weight_oz",
    }


def test_s910_build_batch_resolve_query_all_gid_mode():
    classified = [
        {"idx": 0, "mode": "gid", "gid": _S910_METAFIELD_GID},
        {"idx": 1, "mode": "gid", "gid": _S910_METAFIELD_GID_2},
    ]
    query, variables = catalog_hygiene._build_batch_resolve_query(classified)
    assert query.count("... on Metafield") == 2
    assert variables == {
        "id0": _S910_METAFIELD_GID,
        "id1": _S910_METAFIELD_GID_2,
    }


def test_s910_build_batch_resolve_query_all_triple_mode():
    classified = [
        {
            "idx": 0,
            "mode": "triple",
            "ownerId": _S910_PRODUCT_GID,
            "ownerType": "PRODUCT",
            "namespace": "custom",
            "key": "a",
        },
        {
            "idx": 1,
            "mode": "triple",
            "ownerId": _S910_VARIANT_GID,
            "ownerType": "PRODUCT_VARIANT",
            "namespace": "custom",
            "key": "b",
        },
    ]
    query, variables = catalog_hygiene._build_batch_resolve_query(classified)
    # Two triple-mode selections — each contributes a Product and ProductVariant
    # fragment, so 2 of each.
    assert query.count("... on Product {") == 2
    assert query.count("... on ProductVariant {") == 2
    assert variables == {
        "ownerId0": _S910_PRODUCT_GID,
        "ns0": "custom",
        "k0": "a",
        "ownerId1": _S910_VARIANT_GID,
        "ns1": "custom",
        "k1": "b",
    }


# =============================================================================
# Story 9.11 — get_product_metafields
# =============================================================================
# AC coverage map (see context/get_product_metafields_tool_spec.md):
#   AC #1  identifier validation              → test_s911_no_identifier_rejects
#   AC #2  numeric product_id accepted        → test_s911_numeric_product_id_path
#   AC #3  GID product_id accepted            → test_s911_gid_product_id_path
#   AC #4  handle resolves before fetch       → test_s911_handle_path_resolves_then_fetches
#   AC #5  default returns all namespaces     → test_s911_default_sends_null_filters
#   AC #6  namespace filter                   → test_s911_namespace_filter_passes_through
#   AC #7  namespace + keys filter            → test_s911_namespace_and_keys_filter_passes_through
#   AC #8  keys without namespace pass through (S9.12) → test_s912_keys_alone_pass_through_unqualified
#   AC #9  include_variants=True              → test_s911_include_variants_returns_variant_block
#   AC #10 empty-state, no crash              → test_s911_empty_metafields_no_crash
#   AC #11 product not found                  → test_s911_product_not_found_returns_structured_error
#   AC #12 network exception                  → test_s911_network_exception_returns_structured_error
#   AC #13 pagination                         → test_s911_pagination_concatenates_pages
#   AC #14 google_shopping_pause surfaces     → test_s911_google_shopping_pause_surfaces_in_head_and_tail
#   AC #15 dual output contract               → test_s911_every_path_emits_dual_output
#   AC #16 no new OAuth scopes                → test_s911_happy_path_uses_only_read_query
#   AC #17 coverage gate                      → exercised by branch coverage across all tests
#   AC #18 CI chain green                     → out-of-band (CI verifies)

_S911_PRODUCT_NUMERIC = "8581472649369"
_S911_PRODUCT_GID = "gid://shopify/Product/8581472649369"
_S911_PRODUCT_HANDLE = "all-or-nothing-cypher-tee"
_S911_PRODUCT_TITLE = "All or Nothing Cypher | The Rotation Tee"
_S911_VARIANT_GID = "gid://shopify/ProductVariant/45919117869209"
_S911_METAFIELD_GID = "gid://shopify/Metafield/9000"


def _s911_metafield_node(
    *,
    mid: str = _S911_METAFIELD_GID,
    namespace: str = "google",
    key: str = "age_group",
    value: str = "adult",
    mtype: str = "single_line_text_field",
    created_at: str = "2026-05-16T19:00:00Z",
    updated_at: str = "2026-05-16T19:00:00Z",
) -> dict:
    """Build one metafield-edge node in the Shopify-echo shape."""
    return {
        "id": mid,
        "namespace": namespace,
        "key": key,
        "value": value,
        "type": mtype,
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def _s911_product_response(
    metafields: list[dict] | None = None,
    *,
    has_next: bool = False,
    end_cursor: str | None = None,
    title: str = _S911_PRODUCT_TITLE,
    handle: str = _S911_PRODUCT_HANDLE,
    product_gid: str = _S911_PRODUCT_GID,
) -> dict:
    """Product-only metafields query response (no variants connection)."""
    return {
        "product": {
            "id": product_gid,
            "title": title,
            "handle": handle,
            "metafields": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                "edges": [{"node": n} for n in (metafields or [])],
            },
        }
    }


def _s911_product_with_variants_response(
    metafields: list[dict] | None = None,
    variants: list[dict] | None = None,
    *,
    mf_has_next: bool = False,
    mf_end_cursor: str | None = None,
    variants_has_next: bool = False,
    variants_end_cursor: str | None = None,
    title: str = _S911_PRODUCT_TITLE,
    handle: str = _S911_PRODUCT_HANDLE,
    product_gid: str = _S911_PRODUCT_GID,
) -> dict:
    """Product+variants query response — variants is a list of node dicts."""
    return {
        "product": {
            "id": product_gid,
            "title": title,
            "handle": handle,
            "metafields": {
                "pageInfo": {"hasNextPage": mf_has_next, "endCursor": mf_end_cursor},
                "edges": [{"node": n} for n in (metafields or [])],
            },
            "variants": {
                "pageInfo": {
                    "hasNextPage": variants_has_next,
                    "endCursor": variants_end_cursor,
                },
                "edges": [{"node": v} for v in (variants or [])],
            },
        }
    }


def _s911_variant_node(
    *,
    vid: str = _S911_VARIANT_GID,
    title: str = "Royal / S",
    sku: str = "98665589120164795250",
    metafields: list[dict] | None = None,
) -> dict:
    """One variant-node entry for the include_variants response."""
    return {
        "id": vid,
        "title": title,
        "sku": sku,
        "metafields": {"edges": [{"node": n} for n in (metafields or [])]},
    }


def _s911_handle_lookup_response(
    product_gid: str = _S911_PRODUCT_GID,
) -> dict:
    """Response for the productByHandle resolver used by handle inputs."""
    return {"productByHandle": {"id": product_gid}}


# ----- AC #1 — identifier validation ----------------------------------------


def test_s911_no_identifier_rejects():
    """Calling with neither product_id nor handle returns validation error and
    issues zero GraphQL calls."""
    tools, fc = _build([])
    out = tools["get_product_metafields"]()
    assert "At least one of product_id or handle is required." in out
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["metafields"] == []
    assert any(
        "At least one of product_id or handle is required" in e["message"] for e in tail["errors"]
    )
    assert fc.calls == []


def test_s911_whitespace_only_identifier_rejects():
    """Whitespace-only product_id and handle trip the same gate as empty
    strings — no network calls."""
    tools, fc = _build([])
    out = tools["get_product_metafields"](product_id="   ", handle="\t")
    assert "At least one of product_id or handle is required." in out
    assert fc.calls == []


# ----- AC #2 — numeric product_id accepted ----------------------------------


def test_s911_numeric_product_id_path():
    """Numeric ID is wrapped into a Product GID without an extra resolver call."""
    tools, fc = _build([_s911_product_response([_s911_metafield_node()])])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_NUMERIC)
    assert len(fc.calls) == 1
    query, variables = fc.calls[0]
    assert variables["id"] == _S911_PRODUCT_GID
    assert "GetProductMetafields" in query
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["product"]["id"] == _S911_PRODUCT_GID
    assert tail["totalFound"] == 1


# ----- AC #3 — GID product_id accepted --------------------------------------


def test_s911_gid_product_id_path():
    """Full Product GID passes through unchanged — no resolver round-trip."""
    tools, fc = _build([_s911_product_response([_s911_metafield_node()])])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID)
    assert len(fc.calls) == 1
    _, variables = fc.calls[0]
    assert variables["id"] == _S911_PRODUCT_GID
    tail = _parse_tail(out)
    assert tail["ok"] is True


# ----- AC #4 — handle resolution --------------------------------------------


def test_s911_handle_path_resolves_then_fetches():
    """Handle path: 2 calls — productByHandle then GetProductMetafields."""
    tools, fc = _build(
        [
            _s911_handle_lookup_response(),
            _s911_product_response([_s911_metafield_node()]),
        ]
    )
    out = tools["get_product_metafields"](handle=_S911_PRODUCT_HANDLE)
    assert len(fc.calls) == 2
    first_query, first_vars = fc.calls[0]
    assert "productByHandle" in first_query
    assert first_vars == {"handle": _S911_PRODUCT_HANDLE}
    _, fetch_vars = fc.calls[1]
    assert fetch_vars["id"] == _S911_PRODUCT_GID
    tail = _parse_tail(out)
    assert tail["ok"] is True


def test_s911_handle_not_found_propagates_resolver_error():
    """Handle that productByHandle can't resolve returns a structured error
    before the metafields query is issued."""
    tools, fc = _build([{"productByHandle": None}])
    out = tools["get_product_metafields"](handle="never-was-a-product")
    assert "No product found with handle" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert len(fc.calls) == 1


# ----- AC #5 — default returns all namespaces -------------------------------


def test_s911_default_sends_null_filters():
    """Empty namespace + empty keys translate to GraphQL null variables."""
    tools, fc = _build(
        [
            _s911_product_response(
                [
                    _s911_metafield_node(namespace="google", key="age_group"),
                    _s911_metafield_node(
                        mid="gid://shopify/Metafield/9001",
                        namespace="custom",
                        key="fabric_weight_oz",
                        value="14",
                        mtype="number_integer",
                    ),
                ]
            )
        ]
    )
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID)
    query, variables = fc.calls[0]
    assert "namespace" not in variables
    assert "keys" not in variables
    assert "$namespace" not in query
    assert "$keys" not in query
    tail = _parse_tail(out)
    assert tail["totalFound"] == 2
    assert {m["namespace"] for m in tail["metafields"]} == {"google", "custom"}


# ----- AC #6 — namespace filter ---------------------------------------------


def test_s911_namespace_filter_passes_through():
    """`namespace='google'` is forwarded verbatim; head groups by namespace."""
    tools, fc = _build(
        [
            _s911_product_response(
                [
                    _s911_metafield_node(namespace="google", key="age_group"),
                    _s911_metafield_node(
                        mid="gid://shopify/Metafield/9002",
                        namespace="google",
                        key="gender",
                        value="unisex",
                    ),
                ]
            )
        ]
    )
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, namespace="google")
    query, variables = fc.calls[0]
    assert variables["namespace"] == "google"
    assert "keys" not in variables
    assert "$keys" not in query
    assert "Namespace: google" in out
    assert "• age_group" in out
    assert "• gender" in out


# ----- AC #7 — namespace + keys filter --------------------------------------


def test_s911_namespace_and_keys_filter_passes_through():
    """Both filters supplied → keys are qualified as `<ns>.<key>` and the
    query runs in keys-only mode (S9.12 bug-fix shape)."""
    tools, fc = _build(
        [
            _s911_product_response(
                [
                    _s911_metafield_node(namespace="google", key="age_group"),
                    _s911_metafield_node(
                        mid="gid://shopify/Metafield/9002",
                        namespace="google",
                        key="gender",
                        value="unisex",
                    ),
                ]
            )
        ]
    )
    tools["get_product_metafields"](
        product_id=_S911_PRODUCT_GID,
        namespace="google",
        keys=["age_group", "gender"],
    )
    query, variables = fc.calls[0]
    assert variables["keys"] == ["google.age_group", "google.gender"]
    assert "namespace" not in variables
    assert "$namespace" not in query


def test_s911_keys_with_whitespace_are_stripped_and_empties_dropped():
    """Defensive normalization — whitespace is stripped, empty entries dropped,
    and the surviving keys are qualified with the namespace prefix."""
    tools, fc = _build([_s911_product_response([])])
    tools["get_product_metafields"](
        product_id=_S911_PRODUCT_GID,
        namespace="google",
        keys=["  age_group  ", "", "   ", "gender"],
    )
    _, variables = fc.calls[0]
    assert variables["keys"] == ["google.age_group", "google.gender"]


# ----- AC #8 — keys without namespace (S9.12: now valid, no warning) -------


def test_s912_keys_alone_pass_through_unqualified():
    """Keys without namespace are now valid (S9.12) — they pass through as
    given (callers should supply fully-qualified strings) and no warning
    surfaces. Replaces the deprecated S9.11 warning behavior."""
    tools, fc = _build([_s911_product_response([_s911_metafield_node()])])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, keys=["age_group"])
    assert "keys filter is ignored" not in out
    query, variables = fc.calls[0]
    assert variables["keys"] == ["age_group"]
    assert "namespace" not in variables
    assert "$namespace" not in query
    tail = _parse_tail(out)
    assert tail["ok"] is True


# ----- AC #9 — include_variants=True ----------------------------------------


def test_s911_include_variants_returns_variant_block():
    """`include_variants=True` populates variantMetafields list; default leaves it null."""
    tools, fc = _build(
        [
            _s911_product_with_variants_response(
                metafields=[_s911_metafield_node()],
                variants=[
                    _s911_variant_node(
                        metafields=[
                            _s911_metafield_node(
                                mid="gid://shopify/Metafield/v1",
                                namespace="custom",
                                key="printify_variant_id",
                                value="98665589120164795250",
                            )
                        ]
                    )
                ],
            )
        ]
    )
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, include_variants=True)
    query, variables = fc.calls[0]
    assert "GetProductAndVariantMetafields" in query
    assert variables["variantsFirst"] == 50
    assert variables["variantsAfter"] is None
    tail = _parse_tail(out)
    assert tail["variantMetafields"] is not None
    assert len(tail["variantMetafields"]) == 1
    assert tail["variantMetafields"][0]["variantId"] == _S911_VARIANT_GID
    assert tail["variantMetafields"][0]["sku"] == "98665589120164795250"
    assert tail["totalFound"] == 2  # 1 product + 1 variant
    assert "Variant metafields (1):" in out


def test_s911_include_variants_default_false_keeps_variant_metafields_null():
    """Without include_variants, the JSON tail's variantMetafields stays null."""
    tools, fc = _build([_s911_product_response([_s911_metafield_node()])])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID)
    query, variables = fc.calls[0]
    assert "GetProductAndVariantMetafields" not in query
    assert "variantsFirst" not in variables
    tail = _parse_tail(out)
    assert tail["variantMetafields"] is None


# ----- AC #10 — empty-state, no crash ---------------------------------------


def test_s911_empty_metafields_no_crash():
    """Zero metafields returns ok=true with totalFound=0 and a clean head."""
    tools, _ = _build([_s911_product_response([])])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID)
    assert "No metafields found" in out
    tail = _parse_tail(out)
    assert tail["ok"] is True
    assert tail["metafields"] == []
    assert tail["totalFound"] == 0


def test_s911_empty_metafields_with_namespace_filter_mentions_namespace():
    """Empty result under a namespace filter mentions the namespace in the head."""
    tools, _ = _build([_s911_product_response([])])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, namespace="google")
    assert 'No metafields found for namespace: "google"' in out


# ----- AC #11 — product not found -------------------------------------------


def test_s911_product_not_found_returns_structured_error():
    """Shopify returns `product: null` → structured error response."""
    tools, _ = _build([{"product": None}])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID)
    assert "No product found for the provided ID or handle." in out
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert tail["metafields"] == []


# ----- AC #12 — network exception -------------------------------------------


def test_s911_network_exception_returns_structured_error():
    """Client RuntimeError is caught and reflected in the JSON tail."""
    tools, _ = _build([RuntimeError("Shopify GraphQL error: 503")])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID)
    assert "Error calling Shopify (RuntimeError):" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False
    assert any("503" in e["message"] for e in tail["errors"])


def test_s911_handle_resolver_exception_surfaces_as_resolver_error():
    """An exception raised by the handle resolver becomes a resolver-level error,
    not a "no product" message."""
    tools, _ = _build([RuntimeError("Shopify GraphQL error: 500")])
    out = tools["get_product_metafields"](handle=_S911_PRODUCT_HANDLE)
    assert "Handle lookup failed" in out
    tail = _parse_tail(out)
    assert tail["ok"] is False


# ----- AC #13 — pagination --------------------------------------------------


def test_s911_pagination_concatenates_pages():
    """Two pages of metafields are concatenated; `after` cursor is forwarded
    on the second call."""
    tools, fc = _build(
        [
            _s911_product_response(
                [
                    _s911_metafield_node(mid="gid://shopify/Metafield/p1a", key="age_group"),
                    _s911_metafield_node(mid="gid://shopify/Metafield/p1b", key="gender"),
                ],
                has_next=True,
                end_cursor="CURSOR_PAGE_2",
            ),
            _s911_product_response(
                [
                    _s911_metafield_node(mid="gid://shopify/Metafield/p2a", key="custom_label_0"),
                ],
                has_next=False,
            ),
        ]
    )
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, namespace="google")
    assert len(fc.calls) == 2
    _, page2_vars = fc.calls[1]
    assert page2_vars["after"] == "CURSOR_PAGE_2"
    tail = _parse_tail(out)
    assert tail["totalFound"] == 3
    assert [m["key"] for m in tail["metafields"]] == [
        "age_group",
        "gender",
        "custom_label_0",
    ]


def test_s911_variant_pagination_concatenates_pages():
    """`include_variants` + multi-page variants slice: every variant surfaces
    once across the concatenated pages."""
    tools, fc = _build(
        [
            _s911_product_with_variants_response(
                metafields=[],
                variants=[
                    _s911_variant_node(
                        vid="gid://shopify/ProductVariant/v1",
                        title="Royal / S",
                        sku="SKU-V1",
                        metafields=[
                            _s911_metafield_node(mid="gid://shopify/Metafield/vm1", key="m1")
                        ],
                    )
                ],
                variants_has_next=True,
                variants_end_cursor="VARIANTS_PAGE_2",
            ),
            _s911_product_with_variants_response(
                metafields=[],
                variants=[
                    _s911_variant_node(
                        vid="gid://shopify/ProductVariant/v2",
                        title="Royal / M",
                        sku="SKU-V2",
                        metafields=[
                            _s911_metafield_node(mid="gid://shopify/Metafield/vm2", key="m2")
                        ],
                    )
                ],
            ),
        ]
    )
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, include_variants=True)
    assert len(fc.calls) == 2
    # Iter 1 uses the combined query (both connections still pending).
    iter1_query, _ = fc.calls[0]
    assert "GetProductAndVariantMetafields" in iter1_query
    # Iter 2 switches to the variants-only query — the metafields connection
    # exhausted in iter 1 must not be re-requested (CR suggestion #1 fix).
    iter2_query, page2_vars = fc.calls[1]
    assert "GetProductVariantMetafieldsPage" in iter2_query
    assert "after" not in page2_vars  # metafields cursor not sent on variants-only
    assert page2_vars["variantsAfter"] == "VARIANTS_PAGE_2"
    tail = _parse_tail(out)
    assert len(tail["variantMetafields"]) == 2
    assert {b["variantId"] for b in tail["variantMetafields"]} == {
        "gid://shopify/ProductVariant/v1",
        "gid://shopify/ProductVariant/v2",
    }


def test_s911_pagination_switches_to_metafields_only_query_when_variants_exhaust():
    """Mirror of the variants-only case: when variants exhaust first but
    metafields still need pagination, iter 2 switches to the metafields-only
    query (no variants slice re-requested)."""
    tools, fc = _build(
        [
            _s911_product_with_variants_response(
                metafields=[
                    _s911_metafield_node(mid="gid://shopify/Metafield/p1", key="age_group"),
                ],
                variants=[
                    _s911_variant_node(
                        vid="gid://shopify/ProductVariant/only",
                        metafields=[],
                    )
                ],
                mf_has_next=True,
                mf_end_cursor="MF_PAGE_2",
            ),
            _s911_product_response(
                [_s911_metafield_node(mid="gid://shopify/Metafield/p2", key="gender")],
            ),
        ]
    )
    tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, include_variants=True)
    assert len(fc.calls) == 2
    iter1_query, _ = fc.calls[0]
    assert "GetProductAndVariantMetafields" in iter1_query
    iter2_query, page2_vars = fc.calls[1]
    assert "GetProductMetafields" in iter2_query
    assert "GetProductAndVariantMetafields" not in iter2_query
    assert "variantsFirst" not in page2_vars  # variants slice not re-requested
    assert page2_vars["after"] == "MF_PAGE_2"


# ----- AC #14 — google_shopping_pause surfaces ------------------------------


def test_s911_google_shopping_pause_surfaces_in_head_and_tail():
    """`google.google_shopping_pause = 'all'` appears verbatim in head + JSON."""
    tools, _ = _build(
        [
            _s911_product_response(
                [
                    _s911_metafield_node(
                        namespace="google",
                        key="google_shopping_pause",
                        value="all",
                    )
                ]
            )
        ]
    )
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, namespace="google")
    assert "google_shopping_pause" in out
    assert "→  all" in out
    tail = _parse_tail(out)
    matching = [m for m in tail["metafields"] if m["key"] == "google_shopping_pause"]
    assert len(matching) == 1
    assert matching[0]["value"] == "all"


# ----- AC #15 — dual output contract ----------------------------------------


def test_s911_every_path_emits_dual_output():
    """Validation error, network error, empty state, and success all emit a
    parseable ```json``` tail."""
    # Validation
    tools, _ = _build([])
    assert _parse_tail(tools["get_product_metafields"]())["ok"] is False

    # Network error
    tools, _ = _build([RuntimeError("boom")])
    assert _parse_tail(tools["get_product_metafields"](product_id=_S911_PRODUCT_GID))["ok"] is False

    # Empty state
    tools, _ = _build([_s911_product_response([])])
    empty_tail = _parse_tail(tools["get_product_metafields"](product_id=_S911_PRODUCT_GID))
    assert empty_tail["ok"] is True and empty_tail["metafields"] == []

    # Success
    tools, _ = _build([_s911_product_response([_s911_metafield_node()])])
    success_tail = _parse_tail(tools["get_product_metafields"](product_id=_S911_PRODUCT_GID))
    assert success_tail["ok"] is True and success_tail["totalFound"] == 1


# ----- AC #16 — no new OAuth scopes / happy-path query shape ----------------


def test_s911_happy_path_uses_only_read_query():
    """Happy path executes a single query named GetProductMetafields — no
    mutations, no scope-protected fields. Implicitly confirms the tool stays
    within `read_products`."""
    tools, fc = _build([_s911_product_response([_s911_metafield_node()])])
    tools["get_product_metafields"](product_id=_S911_PRODUCT_GID)
    assert len(fc.calls) == 1
    query, _ = fc.calls[0]
    assert "mutation" not in query.lower()
    assert "GetProductMetafields" in query


# ----- Return-shape pin -----------------------------------------------------


def test_s911_success_json_tail_key_order_pinned():
    """Pin the documented JSON-tail key order so downstream agents can rely
    on consistent traversal: ok, product, metafields, variantMetafields,
    totalFound, errors."""
    tools, _ = _build([_s911_product_response([_s911_metafield_node()])])
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID)
    tail = _parse_tail(out)
    assert list(tail.keys()) == [
        "ok",
        "product",
        "metafields",
        "variantMetafields",
        "totalFound",
        "errors",
    ]
    # Per-metafield key order matches the documented shape too.
    assert list(tail["metafields"][0].keys()) == [
        "id",
        "namespace",
        "key",
        "value",
        "type",
        "createdAt",
        "updatedAt",
    ]


# ----- Helper-level coverage ------------------------------------------------


def test_s911_normalize_filters_helper_empty_namespace_becomes_none():
    mode, ns, keys = catalog_hygiene._normalize_metafield_read_filters("   ", [])
    assert mode == "none"
    assert ns is None and keys is None


def test_s912_normalize_filters_helper_keys_without_namespace_passes_through():
    """S9.12: keys alone are valid (no warning); they pass through as-is
    so the caller can supply fully-qualified strings (`"google.age_group"`)."""
    mode, ns, keys = catalog_hygiene._normalize_metafield_read_filters("", ["age_group"])
    assert mode == "keys"
    assert ns is None
    assert keys == ["age_group"]


def test_s911_normalize_filters_helper_non_list_keys_ignored():
    mode, ns, keys = catalog_hygiene._normalize_metafield_read_filters("google", None)
    assert mode == "namespace"
    assert ns == "google" and keys is None


def test_s911_normalize_filters_helper_non_string_keys_dropped_and_qualified():
    """Non-string entries are dropped; survivors are qualified with the
    namespace prefix so the query can run in keys-only mode."""
    mode, ns, keys = catalog_hygiene._normalize_metafield_read_filters(
        "google", ["age_group", 42, None, "  ", "gender"]
    )
    assert mode == "keys"
    assert ns is None
    assert keys == ["google.age_group", "google.gender"]


def test_s911_group_metafields_by_namespace_preserves_first_seen_order():
    grouped = catalog_hygiene._group_metafields_by_namespace(
        [
            {"namespace": "google", "key": "age_group", "type": "x", "value": "v"},
            {"namespace": "custom", "key": "fabric", "type": "x", "value": "v"},
            {"namespace": "google", "key": "gender", "type": "x", "value": "v"},
        ]
    )
    assert [ns for ns, _ in grouped] == ["google", "custom"]
    assert len(grouped[0][1]) == 2  # two google entries
    assert len(grouped[1][1]) == 1


def test_s911_include_variants_skips_variant_without_metafields_in_head():
    """A variant returned with an empty metafields list is silently skipped in
    the head's per-variant section (still counted in variantMetafields list)."""
    tools, _ = _build(
        [
            _s911_product_with_variants_response(
                metafields=[],
                variants=[
                    _s911_variant_node(
                        vid="gid://shopify/ProductVariant/v-with",
                        title="With Mfs",
                        sku="SKU-WITH",
                        metafields=[
                            _s911_metafield_node(
                                mid="gid://shopify/Metafield/keeper",
                                namespace="custom",
                                key="kept",
                                value="ok",
                            )
                        ],
                    ),
                    _s911_variant_node(
                        vid="gid://shopify/ProductVariant/v-empty",
                        title="Empty Mfs",
                        sku="SKU-EMPTY",
                        metafields=[],
                    ),
                ],
            )
        ]
    )
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, include_variants=True)
    # Both variants surface in the JSON tail (empty array preserved).
    tail = _parse_tail(out)
    skus = {b["sku"] for b in tail["variantMetafields"]}
    assert skus == {"SKU-WITH", "SKU-EMPTY"}
    # But the empty-mfs variant is omitted from the head's per-variant lines.
    assert "Variant With Mfs" in out
    assert "Variant Empty Mfs" not in out


def test_s911_pagination_deduplicates_variant_ids_across_pages():
    """If Shopify echoes the same variant across pages (rare race) the tool
    keeps only the first occurrence — no duplicate entries in the response."""
    duplicated_variant = _s911_variant_node(
        vid="gid://shopify/ProductVariant/dup",
        title="Duplicate",
        sku="SKU-DUP",
        metafields=[
            _s911_metafield_node(
                mid="gid://shopify/Metafield/dup1",
                namespace="custom",
                key="kept",
                value="from-page-1",
            )
        ],
    )
    duplicated_again = _s911_variant_node(
        vid="gid://shopify/ProductVariant/dup",
        title="Duplicate",
        sku="SKU-DUP",
        metafields=[
            _s911_metafield_node(
                mid="gid://shopify/Metafield/dup2",
                namespace="custom",
                key="kept",
                value="from-page-2-IGNORED",
            )
        ],
    )
    tools, _ = _build(
        [
            _s911_product_with_variants_response(
                metafields=[],
                variants=[duplicated_variant],
                variants_has_next=True,
                variants_end_cursor="V_NEXT",
            ),
            _s911_product_with_variants_response(
                metafields=[],
                variants=[duplicated_again],
            ),
        ]
    )
    out = tools["get_product_metafields"](product_id=_S911_PRODUCT_GID, include_variants=True)
    tail = _parse_tail(out)
    # Only the first-seen variant survives the dedup.
    assert len(tail["variantMetafields"]) == 1
    assert tail["variantMetafields"][0]["metafields"][0]["value"] == "from-page-1"


def test_s911_metafield_node_to_dict_preserves_documented_key_order():
    out = catalog_hygiene._metafield_node_to_dict(
        {
            "type": "single_line_text_field",
            "key": "age_group",
            "id": _S911_METAFIELD_GID,
            "value": "adult",
            "namespace": "google",
            "createdAt": "t1",
            "updatedAt": "t2",
        }
    )
    assert list(out.keys()) == [
        "id",
        "namespace",
        "key",
        "value",
        "type",
        "createdAt",
        "updatedAt",
    ]


# =============================================================================
# Story 9.12 — bug fix: namespace + keys conflict on Shopify metafields(...)
# =============================================================================
# Shopify's Admin API rejects the simultaneous presence of `namespace` and
# `keys` on the `metafields(...)` connection — the *declaration* of both args
# in the query string triggers the rejection, not just non-null runtime
# values. Story 9.12 replaces the static query constants with mode-driven
# builders so each call emits only the args its filter mode declares.
#
# Source of truth: context/get_product_metafields_bug_fix.md


def test_s912_namespace_plus_keys_emits_keys_only_query():
    """When both filters were supplied, the emitted query must use keys-only
    mode — neither `$namespace` nor `namespace:` may appear anywhere. The
    keys must be qualified as `<ns>.<key>` so the result is still scoped to
    the requested namespace."""
    tools, fc = _build([_s911_product_response([_s911_metafield_node()])])
    tools["get_product_metafields"](
        product_id=_S911_PRODUCT_GID,
        namespace="google",
        keys=["age_group", "gender"],
    )
    query, variables = fc.calls[0]
    assert "$namespace" not in query
    assert "namespace:" not in query
    assert "$keys" in query
    assert "keys: $keys" in query
    assert variables["keys"] == ["google.age_group", "google.gender"]
    assert "namespace" not in variables


def test_s912_fully_qualified_keys_alone_pass_through():
    """Caller can scope to a single namespace by passing pre-qualified keys
    without a `namespace` argument — keys-only mode runs as-is."""
    tools, fc = _build([_s911_product_response([_s911_metafield_node()])])
    tools["get_product_metafields"](
        product_id=_S911_PRODUCT_GID,
        keys=["google.age_group"],
    )
    query, variables = fc.calls[0]
    assert "$namespace" not in query
    assert "keys: $keys" in query
    assert variables["keys"] == ["google.age_group"]
    assert "namespace" not in variables


def test_s912_no_filters_emits_filter_free_query():
    """No filters → the `metafields(...)` connection must not declare or pass
    `namespace:` or `keys:` arguments at all."""
    tools, fc = _build([_s911_product_response([_s911_metafield_node()])])
    tools["get_product_metafields"](product_id=_S911_PRODUCT_GID)
    query, variables = fc.calls[0]
    assert "$namespace" not in query
    assert "$keys" not in query
    assert "namespace:" not in query
    # `keys:` would only appear as an argument — the `keys` GraphQL field on
    # `MetafieldEdge` does not exist, so this is safe as a substring check.
    assert "keys:" not in query
    assert "namespace" not in variables
    assert "keys" not in variables


def test_s912_include_variants_with_namespace_plus_keys_qualifies_both_connections():
    """The combined product+variants query has TWO `metafields(...)`
    connections (one on `product`, one on each `variant`). S9.12 requires
    both to use the same keys-only mode with qualified keys — Shopify
    enforces the exclusivity rule on each connection independently, so
    leaving either one in the old shape would still 400."""
    tools, fc = _build(
        [
            _s911_product_with_variants_response(
                metafields=[_s911_metafield_node()],
                variants=[
                    _s911_variant_node(
                        metafields=[
                            _s911_metafield_node(
                                mid="gid://shopify/Metafield/v1",
                                namespace="google",
                                key="age_group",
                            )
                        ]
                    )
                ],
            )
        ]
    )
    tools["get_product_metafields"](
        product_id=_S911_PRODUCT_GID,
        namespace="google",
        keys=["age_group"],
        include_variants=True,
    )
    query, variables = fc.calls[0]
    assert "GetProductAndVariantMetafields" in query
    # Neither connection may carry namespace args.
    assert "$namespace" not in query
    assert "namespace:" not in query
    # Both connections (product-level + variant-level) emit `keys: $keys`.
    assert query.count("keys: $keys") == 2
    assert variables["keys"] == ["google.age_group"]
    assert "namespace" not in variables


def test_s912_pagination_with_keys_only_mode_carries_keys_on_page2():
    """Multi-page keys-only request keeps the qualified `keys` and forwards
    the `after:` cursor on the second round-trip — pagination must not
    regress to the old static-query shape that would re-introduce both
    args."""
    tools, fc = _build(
        [
            _s911_product_response(
                [_s911_metafield_node(mid="gid://shopify/Metafield/p1", key="age_group")],
                has_next=True,
                end_cursor="CURSOR_PAGE_2",
            ),
            _s911_product_response(
                [_s911_metafield_node(mid="gid://shopify/Metafield/p2", key="gender")],
                has_next=False,
            ),
        ]
    )
    tools["get_product_metafields"](
        product_id=_S911_PRODUCT_GID,
        namespace="google",
        keys=["age_group", "gender"],
    )
    assert len(fc.calls) == 2
    for page_query, page_vars in fc.calls:
        assert "$namespace" not in page_query
        assert "namespace:" not in page_query
        assert "keys: $keys" in page_query
        assert page_vars["keys"] == ["google.age_group", "google.gender"]
        assert "namespace" not in page_vars
    _, page2_vars = fc.calls[1]
    assert page2_vars["after"] == "CURSOR_PAGE_2"


def test_s912_builders_reject_unknown_filter_mode():
    """Defense in depth: each query builder validates `mode` at entry. A
    future refactor that forgets to thread the closed-enum contract through
    fails loudly here instead of silently emitting a no-filter query."""
    builders = (
        catalog_hygiene._build_get_product_metafields_query,
        catalog_hygiene._build_get_product_and_variant_metafields_query,
        catalog_hygiene._build_get_product_variant_metafields_page_query,
    )
    for builder in builders:
        with pytest.raises(ValueError, match="unknown metafield filter mode"):
            builder("invalid_mode")


def test_s912_empty_keys_mode_lists_qualified_keys_in_head():
    """Empty result under a keys filter lists the qualified keys in the head
    using the same quoted style as the namespace-mode empty message."""
    tools, _ = _build([_s911_product_response([])])
    out = tools["get_product_metafields"](
        product_id=_S911_PRODUCT_GID,
        namespace="google",
        keys=["age_group", "gender"],
    )
    assert 'No metafields found for keys: "google.age_group", "google.gender"' in out
