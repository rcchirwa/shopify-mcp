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
    TAXONOMY_SEARCH,
    UPDATE_PRODUCT_CATEGORY,
)


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    """Keep tests from polluting aon_mcp_log.txt.

    `tools.catalog_hygiene` does `from tools._log import log_write`, so the
    callable lives at the `tools.catalog_hygiene.log_write` attribute. Patch
    the call-site module (not `tools._log`) or the binding won't intercept.
    Shared by 9.3 (update_product_pricing) and 9.1 (update_product_category).
    """
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
    # Pins the current tool set. Future Stories 9.2/9.4-9.7 will grow this;
    # update the assertion alongside each one.
    srv = CapturingServer()
    fc = FakeClient([])
    catalog_hygiene.register(srv, fc)
    assert set(srv.tools.keys()) == {"update_product_pricing", "update_product_category"}
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
    assert "Taxonomy search failed" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
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
    assert "Handle lookup failed" in out
    body = _extract_json_tail(out)
    assert body["ok"] is False
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
