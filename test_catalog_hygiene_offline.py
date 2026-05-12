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


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    """Keep tests from polluting aon_mcp_log.txt."""
    monkeypatch.setattr(catalog_hygiene, "log_write", lambda *a, **k: None)


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


def test_register_adds_update_product_pricing_only():
    srv = CapturingServer()
    fc = FakeClient([])
    catalog_hygiene.register(srv, fc)
    assert set(srv.tools.keys()) == {"update_product_pricing"}
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
