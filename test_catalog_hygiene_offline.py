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

import pytest

from _testing import CapturingServer, FakeClient
from tools import catalog_hygiene
from tools.catalog_hygiene import (
    GET_PRODUCT_BY_HANDLE_MIN,
    GET_PRODUCT_CATEGORY,
    GET_PRODUCT_VENDOR,
    GET_PRODUCT_VENDOR_BY_HANDLE,
    TAXONOMY_SEARCH,
    UPDATE_PRODUCT_CATEGORY,
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
    # Pins the current tool set. Future Stories 9.4-9.7 will grow this;
    # update the assertion alongside each one.
    srv = CapturingServer()
    fc = FakeClient([])
    catalog_hygiene.register(srv, fc)
    assert set(srv.tools.keys()) == {
        "update_product_pricing",
        "update_product_category",
        "update_product_vendor",
        "update_variant_image_binding",
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
    assert "(cleared)" in out


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


def test_update_product_category_best_match_filters_non_leaf_intermediate_nodes():
    # Shopify returns mixed root/intermediate/leaf nodes; we must filter to isLeaf.
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
    # Picked the only leaf, not the root or intermediate.
    assert vars_put["product"]["category"] == "gid://shopify/TaxonomyCategory/aa-1-13-9"


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
# Story 9.6 — update_variant_image_binding
# =============================================================================

from tools._resolvers import GET_PRODUCT_VARIANTS_FOR_RESOLVE  # noqa: E402
from tools.catalog_hygiene import (  # noqa: E402
    GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA,
    PRODUCT_VARIANT_APPEND_MEDIA,
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
    fc_resp = {
        "product": {
            "id": _S96_PRODUCT_GID,
            "variants": {"nodes": [{"id": _S96_VARIANT_A, "sku": "OTHER"}]},
        }
    }
    tools, fc = _build([fc_resp])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "MISSING-SKU", "mediaIds": [_S96_MEDIA_1]}],
    )
    assert out.startswith("Error:")
    assert "MISSING-SKU" in out
    assert len(fc.calls) == 1
    assert fc.calls[0][0] == GET_PRODUCT_VARIANTS_FOR_RESOLVE


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
    assert fc.calls[1][1] == {
        "productId": _S96_PRODUCT_GID,
        "variantMedia": [{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2]}],
    }


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


def test_s96_resolves_sku_to_variant_gid_then_runs_mutation():
    resolver_resp = {
        "product": {
            "id": _S96_PRODUCT_GID,
            "variants": {"nodes": [{"id": _S96_VARIANT_A, "sku": "SKU-A"}]},
        }
    }
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1],
        variants=[(_S96_VARIANT_A, "SKU-A", [])],
    )
    mutation = _s96_mutation_response(
        productVariants=[(_S96_VARIANT_A, [(_S96_MEDIA_1, "", None)])]
    )
    tools, fc = _build([resolver_resp, combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": "SKU-A", "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED" in out
    assert len(fc.calls) == 3
    assert fc.calls[0][0] == GET_PRODUCT_VARIANTS_FOR_RESOLVE
    assert fc.calls[1][0] == GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA
    assert fc.calls[2][0] == PRODUCT_VARIANT_APPEND_MEDIA
    assert fc.calls[2][1]["variantMedia"][0]["variantId"] == _S96_VARIANT_A


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
    assert len(fc.calls[1][1]["variantMedia"]) == 1
    assert fc.calls[1][1]["variantMedia"][0] == {
        "variantId": _S96_VARIANT_A,
        "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2],
    }


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
    assert fc.calls[1][1]["variantMedia"][0]["mediaIds"] == [_S96_MEDIA_1, _S96_MEDIA_2]


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


def test_s96_partial_overlap_only_appends_delta():
    combined = _s96_combined_response(
        media_ids=[_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3],
        variants=[(_S96_VARIANT_A, "SKU-A", [_S96_MEDIA_1])],
    )
    mutation = _s96_mutation_response(
        productVariants=[
            (
                _S96_VARIANT_A,
                [(_S96_MEDIA_1, "", None), (_S96_MEDIA_2, "", None), (_S96_MEDIA_3, "", None)],
            )
        ]
    )
    tools, fc = _build([combined, mutation])
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[
            {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1, _S96_MEDIA_2, _S96_MEDIA_3]}
        ],
        confirm=True,
    )
    assert "CONFIRMED" in out
    assert fc.calls[1][1]["variantMedia"] == [
        {"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_2, _S96_MEDIA_3]}
    ]


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


def test_s96_no_op_confirmed_surfaces_bound_mid_not_in_product_media_index():
    # Variant has bound media beyond the product-media `first: 100` window —
    # JSON tail falls back to `{"id": mid}` for that node.
    far_mid = "gid://shopify/MediaImage/9999"
    combined = {
        "product": {
            "id": _S96_PRODUCT_GID,
            "title": "T",
            "media": {"nodes": [{"id": _S96_MEDIA_1, "alt": None, "mediaContentType": "IMAGE"}]},
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
    out = tools["update_variant_image_binding"](
        product_id=_S96_PRODUCT_GID,
        variant_media=[{"variantId": _S96_VARIANT_A, "mediaIds": [_S96_MEDIA_1]}],
        confirm=True,
    )
    assert "CONFIRMED — Bind variant images (no-op)" in out
    tail = _parse_tail(out)
    ids_in_tail = {m["id"] for m in tail["variants"][0]["media"]}
    assert ids_in_tail == {far_mid, _S96_MEDIA_1}
    # far_mid's node has alt/image None (came from fallback dict).
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


def test_s96_preview_json_tail_shape():
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
                "currentMedia": [_S96_MEDIA_1],
                "wouldAppend": [_S96_MEDIA_2],
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
