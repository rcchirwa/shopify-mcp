"""
Catalog-hygiene tools — Epic 9 (Stories 9.1-9.7).

Wave 0 (this file at story-open): empty `register()` skeleton + the convention
contract below. Each Wave 1/2 story plugs its tool function into `register()`
without touching shopify_mcp.py again.

Write-tool convention — `confirm`, not `dryRun`:
    Trello card ualcSqFq pre-start gate: the spec's `dryRun: boolean` wording is
    NOT followed here. All write tools in this module use
    `confirm: bool = False` to match the existing 22-tool codebase convention
    (see tools/products.py:303 for the canonical template). Same semantics —
    `confirm=False` returns a preview, `confirm=True` executes — just the
    name the rest of the codebase already uses.

Return shape — human-readable head + fenced ```json``` tail:
    Per the spec amendment at shopify-aon-mcp-catalog-tools-spec.md:48, every
    return path emits two things, in order:
      1. A human-readable line/block in the existing tools/products.py style
         (PREVIEW —, CONFIRMED —, Error:, "No product found …").
      2. A fenced ```json``` block containing the documented per-tool payload
         (`{ok, …, errors}`). Downstream agents parse this tail; humans /
         LLMs-in-the-loop read the head.
    Use `_render(head, payload)` for every return — it guarantees the dual
    output and keeps the JSON serialization consistent across tools.

Story 9.6 (`update_variant_image_binding`) known limitations:
    - product_id accepts numeric ID or Product GID only — handle resolution
      is not wired through `_as_product_gid` (T-9.6-handle).
    - GraphQL pagination caps: 100 product media, 250 variants, 100 media
      per variant. A media GID past the first 100 product-media nodes would
      be falsely rejected as not-on-product (T-9.6-media-cap).
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.server.fastmcp import FastMCP

from shopify_client import (
    ShopifyClient,
    extract_user_errors,
    format_user_errors,
    from_gid,
    to_gid,
    with_confirm_hint,
)
from tools._log import log_write
from tools._resolvers import resolve_variant_ids_with_variants
from tools.media._common import _as_product_gid

# Page cap mirrors `productVariantsBulkUpdate`'s 250-variant window; same idiom
# as tools/products.py:247. A product hitting this cap would need paginated
# reads + chunked bulk updates, which Story 9.3 does not implement.
VARIANTS_PAGE_CAP = 250

GET_PRODUCT_VARIANTS_FOR_PRICING = """
query GetProductVariantsForPricing($id: ID!) {
  product(id: $id) {
    id
    title
    variants(first: 250) {
      nodes { id sku price compareAtPrice }
    }
  }
}
"""

UPDATE_PRODUCT_VARIANTS_PRICING = """
mutation UpdateProductVariantsPricing(
  $productId: ID!,
  $variants: [ProductVariantsBulkInput!]!
) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    product { id }
    productVariants { id sku price compareAtPrice }
    userErrors { field message }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL — Story 9.2 (update_product_vendor)
# ---------------------------------------------------------------------------

# Vendor lookup is intentionally lighter than the products.py GET_PRODUCT_BY_ID
# query — only the fields needed for the preview text and the idempotency
# check. Cuts the read-path cost roughly in half vs. reusing the heavier query.
GET_PRODUCT_VENDOR = """
query GetProductVendor($id: ID!) {
  product(id: $id) {
    id
    title
    vendor
  }
}
"""

# Handle-form resolver: only fired when the caller passes a non-numeric,
# non-GID identifier. Returns the GID + the same vendor/title fields so the
# preview path doesn't need a second round-trip after resolution.
GET_PRODUCT_VENDOR_BY_HANDLE = """
query GetProductVendorByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    vendor
  }
}
"""

# Per spec: ProductUpdateInput (not the older `input: ProductInput!` shape used
# in tools/products.py). Documented in shopify-aon-mcp-catalog-tools-spec.md
# §"Tool 2 — Underlying Shopify GraphQL".
UPDATE_PRODUCT_VENDOR = """
mutation productUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id vendor }
    userErrors { field message }
  }
}
"""

# Shopify rejects vendor strings longer than 255 characters; enforce the cap
# client-side so the userError comes back as a structured tool error rather
# than a Shopify userError with a less descriptive message.
VENDOR_MAX_LEN = 255

# ---------------------------------------------------------------------------
# GraphQL — Story 9.4 (update_product_type)
# ---------------------------------------------------------------------------

# Narrow read for the productType field — mirror of GET_PRODUCT_VENDOR. Kept
# separate from the vendor query so a future API-version bump on either field
# doesn't have to untangle them.
GET_PRODUCT_TYPE = """
query GetProductType($id: ID!) {
  product(id: $id) {
    id
    title
    productType
  }
}
"""

# Handle-form resolver — only fired when product_id is non-numeric and non-GID.
GET_PRODUCT_TYPE_BY_HANDLE = """
query GetProductTypeByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    productType
  }
}
"""

# ProductUpdateInput shape — same input variable as the vendor mutation. Per
# spec, clearing productType is wire-encoded as `productType: ""` (Shopify
# treats empty string as cleared for this field, unlike vendor where null is
# the clear path).
UPDATE_PRODUCT_TYPE = """
mutation productUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id productType }
    userErrors { field message }
  }
}
"""

# Same 255-char cap that vendor enforces. Shopify rejects longer values; we
# pre-empt with a structured tool error.
PRODUCT_TYPE_MAX_LEN = 255

# ---------------------------------------------------------------------------
# GraphQL — Story 9.1 (update_product_category)
# ---------------------------------------------------------------------------

# Look up a Product when only its `handle` is known. Story 9.1 accepts numeric
# ID / GID / handle for `productId`; the first two short-circuit via to_gid,
# the handle path needs a query.
GET_PRODUCT_BY_HANDLE_MIN = """
query GetProductByHandleMin($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    category { id fullName name }
  }
}
"""

# Read current category for idempotency check + post-write snapshot. Narrower
# than tools.products.GET_PRODUCT_BY_ID — we only need id/title/category here.
GET_PRODUCT_CATEGORY = """
query GetProductCategory($id: ID!) {
  product(id: $id) {
    id
    title
    category { id fullName name }
  }
}
"""

# Shopify's Standard Product Taxonomy search. Returns up to 10 ranked nodes —
# rank is the order Shopify returns; there is no explicit `score` field.
TAXONOMY_SEARCH = """
query taxonomyCategories($search: String!) {
  taxonomy {
    categories(search: $search, first: 10) {
      nodes { id fullName name level isLeaf isRoot }
    }
  }
}
"""

# 2025+ Admin API takes `ProductUpdateInput` (singular `product:` argument);
# the legacy `ProductInput` shape is what `tools/products.py` still uses for
# title/handle/seo writes. Story 9.1 spec pins the new shape — keep them
# distinct so a future API-version bump doesn't have to untangle them.
UPDATE_PRODUCT_CATEGORY = """
mutation productUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product {
      id
      title
      category { id fullName name }
    }
    userErrors { field message }
  }
}
"""

_VALID_RESOLVE_STRATEGIES = ("exact", "best-match", "reject-ambiguous")
_TAXONOMY_GID_PREFIX = "gid://shopify/TaxonomyCategory/"
_PRODUCT_GID_PREFIX = "gid://shopify/Product/"

# ---------------------------------------------------------------------------
# GraphQL — Story 9.6 (update_variant_image_binding)
# ---------------------------------------------------------------------------

# Combined fetch: product media set (cross-product validation, AC #4) AND
# per-variant currently-bound media (idempotent detection, AC #6) in one
# round-trip. The SKU index also rides along — so if the caller mixes SKUs
# in, no extra resolver fetch is needed (the variants list is already here).
GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA = """
query GetProductMediaAndVariantMedia($id: ID!) {
  product(id: $id) {
    id
    title
    media(first: 100) {
      nodes {
        id
        alt
        mediaContentType
        ... on MediaImage { image { url } }
      }
    }
    variants(first: 250) {
      nodes {
        id
        sku
        media(first: 100) { nodes { id } }
      }
    }
  }
}
"""

PRODUCT_VARIANT_APPEND_MEDIA = """
mutation ProductVariantAppendMedia(
  $productId: ID!,
  $variantMedia: [ProductVariantAppendMediaInput!]!
) {
  productVariantAppendMedia(productId: $productId, variantMedia: $variantMedia) {
    productVariants {
      id
      media(first: 100) {
        nodes {
          id
          alt
          mediaContentType
          ... on MediaImage { image { url } }
        }
      }
    }
    userErrors { field message }
  }
}
"""

# ---------------------------------------------------------------------------
# GraphQL + constants — Story 9.7 (set_product_metafields)
# ---------------------------------------------------------------------------

# Shopify's `metafieldsSet` accepts up to 25 entries per call. Enforced
# client-side so the cap-exceeded path returns a structured tool error
# rather than a less-descriptive Shopify userError.
METAFIELDS_SET_MAX = 25

# Reserved namespace for app-context metafields. Setting an `app--*` metafield
# requires the metafield owner app's token; rejecting client-side prevents a
# guaranteed Shopify rejection.
RESERVED_NAMESPACE_PREFIX = "app--"

# Story 9.7 scope: metafield owners are limited to Product and ProductVariant.
# Other owner types (Collection, Customer, Order, etc.) are out of scope —
# adding them would require deciding their resolver / preview strategy.
METAFIELD_OWNER_PREFIXES: tuple[str, str] = (
    "gid://shopify/Product/",
    "gid://shopify/ProductVariant/",
)

# Owner-prefix → Shopify ownerType enum string. Only the two supported types
# are mapped; lookup failure is a programmer error caught by `_parse_owner_gid`.
_OWNER_TYPE_BY_PREFIX: dict[str, str] = {
    "gid://shopify/Product/": "PRODUCT",
    "gid://shopify/ProductVariant/": "PRODUCT_VARIANT",
}

# Curated set of Shopify metafield types this tool *shape-checks* client-side.
# Spec line 506 calls for "basic regex check for numeric types, JSON.parse
# check for JSON / list.* types" — keep the set small and predictable. Any
# `type` value NOT in this frozenset is passed through to Shopify with no
# client-side shape check (forward-compatible: Shopify adds new types and we
# don't want the tool to gate on every API version bump).
SUPPORTED_METAFIELD_TYPES: frozenset[str] = frozenset(
    [
        "single_line_text_field",
        "multi_line_text_field",
        "number_integer",
        "number_decimal",
        "boolean",
        "date",
        "date_time",
        "url",
        "color",
        "json",
        "rich_text_field",
        "list.single_line_text_field",
        "list.number_integer",
        "list.number_decimal",
        "list.url",
        "list.color",
        "list.date",
        "list.date_time",
    ]
)

# Currently-granted OAuth scopes per CLAUDE.md §"Shopify Admin API scopes".
# Surfaced in the ACCESS_DENIED `remediation` payload so the caller / merchant
# can see exactly what's available when deciding whether to re-grant. Keep
# in lockstep with CLAUDE.md:13-19 — if scopes are added or removed there,
# update this constant in the same PR.
GRANTED_SCOPES_HINT = (
    "read_products, write_products, read_inventory, write_inventory, "
    "read_orders, read_price_rules, write_price_rules, "
    "read_discounts, write_discounts, "
    "read_publications, write_publications, write_files"
)

# `userErrors { code }` is REQUIRED here (vs. just `field message` in the
# other Epic 9 mutations) so the ACCESS_DENIED branch in AC #10 can detect
# the scope-block signal without falling back to message-string matching.
METAFIELDS_SET_MUTATION = """
mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields {
      id
      namespace
      key
      value
      type
      ownerType
    }
    userErrors { field message code }
  }
}
"""

# Regex shape-checks for numeric metafield types. Anchored on both ends so a
# leading minus is allowed but trailing junk ("14abc", "14 ") is rejected.
_NUMBER_INTEGER_RE = re.compile(r"^-?\d+$")
_NUMBER_DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$")

# ---------------------------------------------------------------------------
# GraphQL + constants — Story 9.5 (update_product_options)
# ---------------------------------------------------------------------------

# Narrow read: option + value GIDs for the tool-side child-of-option validation
# AND the post-write snapshot's variants slice. The variants(first: 50) cap
# matches `get_product_full` in tools/products.py — products beyond that need
# pagination (tracked as T-9.5-variants-cap).
GET_PRODUCT_OPTIONS = """
query GetProductOptions($id: ID!) {
  product(id: $id) {
    id
    title
    options {
      id
      name
      optionValues { id name }
    }
    variants(first: 50) {
      nodes { id title selectedOptions { name value } }
    }
  }
}
"""

# Handle-form resolver — fired only when product_id is non-numeric and non-GID.
# Returns the same shape as GET_PRODUCT_OPTIONS so callers don't branch on the
# read result.
GET_PRODUCT_OPTIONS_BY_HANDLE = """
query GetProductOptionsByHandle($handle: String!) {
  productByHandle(handle: $handle) {
    id
    title
    options {
      id
      name
      optionValues { id name }
    }
    variants(first: 50) {
      nodes { id title selectedOptions { name value } }
    }
  }
}
"""

# `productOptionUpdate` updates exactly one option per call (Shopify limit).
# The tool signature only accepts a single `option` arg, so the multi-option
# case is impossible to express — see AC #10. `userErrors { code }` is required
# here so a DUPLICATE_OPTION_VALUE_NAME (rename collision) is surfacable as a
# structured error rather than message-string matching.
UPDATE_PRODUCT_OPTION = """
mutation productOptionUpdate(
  $productId: ID!,
  $option: OptionUpdateInput!,
  $optionValuesToUpdate: [OptionValueUpdateInput!],
  $variantStrategy: ProductOptionUpdateVariantStrategy
) {
  productOptionUpdate(
    productId: $productId,
    option: $option,
    optionValuesToUpdate: $optionValuesToUpdate,
    variantStrategy: $variantStrategy
  ) {
    product {
      id
      options { id name optionValues { id name } }
      variants(first: 50) { nodes { id title selectedOptions { name value } } }
    }
    userErrors { field message code }
  }
}
"""

# Shopify caps option names at 255 chars (Admin schema docstring). Same value
# as VENDOR_MAX_LEN / PRODUCT_TYPE_MAX_LEN; kept distinct so a future divergence
# can change one without touching the others.
OPTION_NAME_MAX_LEN = 255

# Default per spec is LEAVE_AS_IS — keeps existing variants pointing at the
# renamed values. MANAGE lets Shopify reconcile (rarely needed for renames;
# useful for option-shape changes). Default is loud in the docstring per the
# spec edge-case warning about unexpected variant deduplication.
_VALID_VARIANT_STRATEGIES: tuple[str, str] = ("LEAVE_AS_IS", "MANAGE")

_PRODUCT_OPTION_GID_PREFIX = "gid://shopify/ProductOption/"
_PRODUCT_OPTION_VALUE_GID_PREFIX = "gid://shopify/ProductOptionValue/"


def _is_already_bound_error(message: str) -> bool:
    """Detect Shopify's 'already bound' family of userErrors.

    Pre-filtering empty deltas prevents the error in the common case, but a
    parallel call against the same product can race past the read — treat
    those errors as success per AC #6 (idempotent re-bind).
    """
    lower = (message or "").lower()
    return "already" in lower and ("bound" in lower or "associated" in lower)


def _media_node_to_json(node: dict[str, Any]) -> dict[str, Any]:
    """Serialize a media node into the spec-aligned JSON-tail shape.

    Keys: `id`, `alt`, `mediaContentType`, `image` (object with `url` or
    None). Missing fields surface as None — JSON consumers should treat
    these as "unknown" rather than literal empties.
    """
    image = node.get("image")
    return {
        "id": node.get("id"),
        "alt": node.get("alt"),
        "mediaContentType": node.get("mediaContentType"),
        "image": {"url": (image or {}).get("url")} if image else None,
    }


def _render(head: str, payload: dict[str, Any]) -> str:
    """Format the dual-output return: human-readable head + fenced JSON tail.

    Every tool return in this module funnels through here so the head/tail
    contract stays consistent. `indent=2` keeps the JSON readable when an
    LLM relays the response inline; `sort_keys=False` preserves insertion
    order so callers can rely on `id` appearing before `sku` etc.
    """
    return f"{head}\n\n```json\n{json.dumps(payload, indent=2)}\n```"


def _err_payload(message: str, *, key: str = "variants") -> dict[str, Any]:
    """Build the JSON tail for a tool-side error (validation, resolver, etc.).

    Shape mirrors the success payload's `errors` slot but with `ok: false`
    and an empty data list under the per-tool key. Validation errors have
    no Shopify `field` path, so the entry carries `message` only.

    `key` defaults to `"variants"` for backwards-compat with Story 9.6
    (`update_variant_image_binding`). Story 9.7 calls with `key="metafields"`.
    """
    return {"ok": False, key: [], "errors": [{"message": message}]}


def _parse_owner_gid(gid: object) -> tuple[str | None, str | None]:
    """Parse a metafield owner GID into (ownerType, error).

    Accepts only Product / ProductVariant GIDs per Story 9.7 scope. Returns
    `(ownerType, None)` on the happy path where `ownerType` is the Shopify
    enum string (`"PRODUCT"` / `"PRODUCT_VARIANT"`). Returns `(None, msg)`
    on a malformed or out-of-scope GID — the GID prefix decides the
    ownerType; the numeric tail is left to Shopify to reject if invalid.
    """
    if not isinstance(gid, str) or not gid.strip():
        return None, "ownerId must be a non-empty string"
    stripped = gid.strip()
    for prefix in METAFIELD_OWNER_PREFIXES:
        if stripped.startswith(prefix):
            if not stripped[len(prefix) :]:
                return None, f"ownerId has empty GID body: {stripped!r}"
            return _OWNER_TYPE_BY_PREFIX[prefix], None
    return None, (f"ownerId must be a Product or ProductVariant GID (got {stripped!r})")


def _validate_metafield_value(value: str, mtype: str) -> str | None:
    """Light-touch shape check for known metafield types.

    Returns None on shape OK (or for an unknown type — those pass through
    to Shopify for validation per the curated-set rationale at
    SUPPORTED_METAFIELD_TYPES). Returns a single-line error string when
    the value is shape-incompatible with the type.
    """
    if mtype == "number_integer":
        if not _NUMBER_INTEGER_RE.match(value):
            return f"value {value!r} is not a valid integer for type 'number_integer'"
        return None
    if mtype == "number_decimal":
        if not _NUMBER_DECIMAL_RE.match(value):
            return f"value {value!r} is not a valid decimal for type 'number_decimal'"
        return None
    if mtype == "boolean":
        if value not in ("true", "false"):
            return f"value {value!r} must be 'true' or 'false' for type 'boolean'"
        return None
    if mtype == "json":
        try:
            json.loads(value)
        except (ValueError, TypeError):
            return f"value {value!r} is not valid JSON for type 'json'"
        return None
    if mtype.startswith("list."):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return (
                f"value {value!r} is not valid JSON for list-type {mtype!r} "
                f"(expected a JSON-serialized array string)"
            )
        if not isinstance(parsed, list):
            return (
                f"value for list-type {mtype!r} must decode to a JSON array "
                f"(got {type(parsed).__name__})"
            )
        return None
    # Other supported types (text, url, color, date, date_time, rich_text_field)
    # and unknown types: no client-side shape check — Shopify validates.
    return None


def _normalize_metafield_entries(
    entries: list,
) -> tuple[list[dict[str, Any]] | None, dict[int, list[str]]]:
    """Per-entry validate + normalize the `metafields` input.

    Returns `(normalized_list, errors_by_index)`:
      - On all-entries-valid: `(normalized, {})` where each normalized dict
        is `{ownerId, namespace, key, value, type, ownerType}` ready to ship
        to Shopify. The added `ownerType` is *internal* — stripped from the
        mutation input but kept for the preview payload's per-entry display.
      - On any entry invalid: `(None, errors_by_index)` where the dict maps
        each failing entry index to a list of error strings (one entry can
        have multiple errors, e.g. bad ownerId AND bad value).
    """
    normalized: list[dict[str, Any]] = []
    errors_by_index: dict[int, list[str]] = {}

    def _push(idx: int, msg: str) -> None:
        errors_by_index.setdefault(idx, []).append(msg)

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _push(idx, f"metafields[{idx}] must be an object")
            continue

        owner_id_raw = entry.get("ownerId")
        owner_type, owner_err = _parse_owner_gid(owner_id_raw)
        if owner_err:
            _push(idx, f"metafields[{idx}].{owner_err}")

        ns = entry.get("namespace")
        if not isinstance(ns, str) or not ns.strip():
            _push(idx, f"metafields[{idx}].namespace must be a non-empty string")
            ns_clean: str | None = None
        else:
            ns_clean = ns.strip()
            if ns_clean.startswith(RESERVED_NAMESPACE_PREFIX):
                _push(
                    idx,
                    f"metafields[{idx}].namespace {ns_clean!r} uses the reserved "
                    f"'{RESERVED_NAMESPACE_PREFIX}' prefix (app-context only)",
                )

        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            _push(idx, f"metafields[{idx}].key must be a non-empty string")
            key_clean: str | None = None
        else:
            key_clean = key.strip()

        mtype = entry.get("type")
        if not isinstance(mtype, str) or not mtype.strip():
            _push(idx, f"metafields[{idx}].type must be a non-empty string")
            mtype_clean: str | None = None
        else:
            mtype_clean = mtype.strip()

        value = entry.get("value")
        if not isinstance(value, str):
            _push(
                idx,
                f"metafields[{idx}].value must be a string "
                f"(JSON-serialized for json / list.* types)",
            )
            value_clean: str | None = None
        else:
            value_clean = value
            if mtype_clean:
                shape_err = _validate_metafield_value(value_clean, mtype_clean)
                if shape_err:
                    _push(idx, f"metafields[{idx}].{shape_err}")

        if idx not in errors_by_index:
            # All per-entry checks passed — every local is a non-None str /
            # known ownerType at this point. The `if idx not in errors_by_index`
            # guard above is the runtime proof that no validation branch ran, so
            # these asserts can never fire in production; they exist solely as
            # mypy invariant pins. Under `python -O` they are stripped, which is
            # safe for exactly this reason.
            assert owner_type is not None
            assert ns_clean is not None
            assert key_clean is not None
            assert mtype_clean is not None
            assert value_clean is not None
            assert isinstance(owner_id_raw, str)
            normalized.append(
                {
                    "ownerId": owner_id_raw.strip(),
                    "namespace": ns_clean,
                    "key": key_clean,
                    "value": value_clean,
                    "type": mtype_clean,
                    # `ownerType` is for preview display only — stripped before
                    # the mutation call (Shopify infers it from `ownerId`).
                    "ownerType": owner_type,
                }
            )

    if errors_by_index:
        return None, errors_by_index
    return normalized, {}


def _format_metafields_payload(
    *,
    metafields: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    ok: bool,
    preview: bool,
    remediation: str | None = None,
    errors_by_index: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """Serialize the JSON tail block for set_product_metafields.

    Returns ONLY the fenced ```json``` block — the caller composes the
    human-readable head. Parallel to `_format_vendor_payload` (Story 9.2)
    rather than `_render` (which wraps head + tail in one call) so that
    the head wording can vary across validation / preview / mutation /
    ACCESS_DENIED / userError paths without ferrying the head through
    multiple helper signatures.
    """
    payload: dict[str, Any] = {
        "ok": ok,
        "metafields": metafields,
        "errors": errors,
        "preview": preview,
    }
    if errors_by_index:
        payload["errorsByIndex"] = errors_by_index
    if remediation:
        payload["remediation"] = remediation
    return "```json\n" + json.dumps(payload, indent=2) + "\n```"


def _parse_positive_decimal(raw: object) -> Decimal:
    """Parse a strict-positive monetary value with ≤ 2 decimal places.

    Rejects negatives, zero, non-strings, NaN/inf, and >2 decimal places.
    AC 3 says "positive decimals" — read strictly per the plan. Raises
    ValueError with a readable message; callers turn this into `Error: ...`.
    """
    if not isinstance(raw, str):
        raise ValueError(f"{raw!r} is not a string")
    stripped = raw.strip()
    if not stripped:
        raise ValueError("price/compareAtPrice must be a non-empty string")
    try:
        value = Decimal(stripped)
    except InvalidOperation as exc:
        raise ValueError(f"{raw!r} is not a positive decimal") from exc
    if not value.is_finite():
        raise ValueError(f"{raw!r} is not a positive decimal")
    if value <= 0:
        raise ValueError(f"{raw!r} is not a positive decimal")
    # Reject >2 decimal places via quantize-roundtrip: 49.99 → 49.99 (equal),
    # 49.999 → 50.00 (not equal). Equality on Decimal is value-based, so
    # 49.9 / 49.90 / 49.900 all compare equal to their 2-place quantization.
    if value != value.quantize(Decimal("0.01")):
        raise ValueError(f"{raw!r} has more than 2 decimal places")
    return value


def _normalize_entries(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate caller's variants[] and return normalized per-entry dicts.

    Key presence encodes intent for each field — downstream code uses
    `"<field>" in entry` to decide whether to touch it:
      variantId: str (always present, stripped)
      price: str (present only if caller supplied it; validated, stripped)
      compareAtPrice: str | None (present only if caller supplied; None
        means "clear the field", a string means "set to this value")

    Rejects: empty list, non-dict entries, missing variantId, entries with
    neither price nor compareAtPrice, invalid decimals, and duplicate
    variantId across entries (merging would be ambiguous — caller should
    coalesce upstream).

    Raises ValueError on any validation failure; the whole call is rejected.
    """
    if not isinstance(variants, list) or not variants:
        raise ValueError("variants must be a non-empty list")

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(variants):
        if not isinstance(entry, dict):
            raise ValueError(f"variants[{i}] must be an object")

        variant_id = entry.get("variantId")
        if not isinstance(variant_id, str) or not variant_id.strip():
            raise ValueError(f"variants[{i}].variantId is required")
        trimmed_id = variant_id.strip()
        if trimmed_id in seen_ids:
            raise ValueError(
                f"variants[{i}].variantId {trimmed_id!r} is a duplicate; "
                "merge price/compareAtPrice into a single entry"
            )
        seen_ids.add(trimmed_id)

        has_price = "price" in entry
        has_cap = "compareAtPrice" in entry
        if not has_price and not has_cap:
            raise ValueError(f"variants[{i}] must supply price or compareAtPrice")

        normalized: dict[str, Any] = {"variantId": trimmed_id}

        if has_price:
            try:
                _parse_positive_decimal(entry["price"])
            except ValueError as exc:
                raise ValueError(f"variants[{i}].price {exc}") from exc
            normalized["price"] = entry["price"].strip()

        if has_cap:
            raw_cap = entry["compareAtPrice"]
            if raw_cap is None:
                normalized["compareAtPrice"] = None  # explicit clear
            else:
                try:
                    _parse_positive_decimal(raw_cap)
                except ValueError as exc:
                    raise ValueError(f"variants[{i}].compareAtPrice {exc}") from exc
                normalized["compareAtPrice"] = raw_cap.strip()

        out.append(normalized)

    return out


def _format_old_new(label: str, old: Any, new: Any) -> str:
    """Render a single field's old → new line, normalizing None → '(cleared)'."""
    old_disp = "(none)" if old is None else str(old)
    new_disp = "(cleared)" if new is None else str(new)
    return f"      {label}: {old_disp} → {new_disp}"


def _entry_matches_existing(
    target_price: str | None,
    target_cap: Any,
    has_cap: bool,
    existing: dict[str, Any],
) -> bool:
    """True if this entry's requested state already matches the variant."""
    if target_price is not None and Decimal(target_price) != Decimal(existing.get("price") or "0"):
        return False
    if has_cap:
        existing_cap = existing.get("compareAtPrice")
        if target_cap is None:
            if existing_cap is not None:
                return False
        else:
            if existing_cap is None or Decimal(target_cap) != Decimal(existing_cap):
                return False
    return True


def _project_variant(entry: dict[str, Any], existing: dict[str, Any], gid: str) -> dict[str, Any]:
    """Build the projected post-state for a single resolved variant.

    Combines caller's targets with the existing read — unchanged fields
    keep their existing value; `compareAtPrice: None` from the caller maps
    to a JSON null in the tail (the spec's "clear" signal).
    """
    target_price = entry["price"] if "price" in entry else existing.get("price")
    target_cap: Any
    if "compareAtPrice" in entry:
        target_cap = entry["compareAtPrice"]  # None = cleared
    else:
        target_cap = existing.get("compareAtPrice")
    return {
        "id": gid,
        "sku": existing.get("sku"),
        "price": target_price,
        "compareAtPrice": target_cap,
    }


# ---------------------------------------------------------------------------
# Helpers — Story 9.1 (update_product_category)
# ---------------------------------------------------------------------------


def _format_payload(
    header: str,
    payload: dict[str, Any],
    *,
    confirm_hint: bool,
) -> str:
    """Render the hybrid string-preview + JSON-tail output for Story 9.1.

    `header` is the human-readable section above the JSON. `payload` is the
    structured response dict per the spec's "Return shape". `confirm_hint`
    controls whether the trailing "Reply with confirm=True…" line appears
    (only in the preview branch). Distinct from `_render` (Story 9.3) since
    the two tools emit slightly different head/tail compositions.
    """
    body = header
    if confirm_hint:
        body += "\n\nReply with confirm=True to execute."
    return f"{body}\n\n```json\n{json.dumps(payload)}\n```"


def _resolve_product_gid(
    client: ShopifyClient,
    product_id: str,
) -> tuple[str | None, str | None]:
    """Map a numeric ID / GID / handle to a Product GID.

    Returns (gid, error). On success, error is None. On failure (handle not
    found, malformed input, transport failure), gid is None and error is a
    human-readable string. Numeric and GID inputs short-circuit without a
    network call; handle inputs trigger a `productByHandle` query (wrapped
    in try/except so non-200 HTTP surfaces as a structured error per AC #8).
    """
    if not isinstance(product_id, str) or not product_id.strip():
        return None, "product_id must be a non-empty string."
    stripped = product_id.strip()

    if stripped.startswith(_PRODUCT_GID_PREFIX):
        if not stripped[len(_PRODUCT_GID_PREFIX) :]:
            return None, f"Empty product GID body: {stripped!r}"
        return stripped, None

    if stripped.isdigit():
        return to_gid("Product", stripped), None

    # Treat anything else as a handle. Shopify handles are lowercase
    # alphanumerics/hyphens/underscores — but rather than gate here, we let
    # the GraphQL query decide (returns null for unknown handles).
    try:
        data = client.execute(GET_PRODUCT_BY_HANDLE_MIN, {"handle": stripped})
    except Exception as e:
        return None, f"Handle lookup failed ({type(e).__name__}): {e}"
    product = (data or {}).get("productByHandle")
    if not product:
        return None, f"No product found with handle {stripped!r}."
    return product["id"], None


def _resolve_taxonomy_category(
    client: ShopifyClient,
    category: str,
    resolve_strategy: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """Resolve a free-text search string (or GID passthrough) to a category node.

    Returns (chosen_node, alternates, error). `chosen_node` is the dict
    {id, fullName, name, ...} that will be sent to productUpdate. `alternates`
    are runner-up candidates (only populated under best-match). When a
    TaxonomyCategory GID is supplied, no search is performed: chosen_node has
    only the `id` set so the caller still sees a stable shape.

    Candidate set includes leaves AND non-leaves (root + intermediate nodes).
    Story 9.8 dropped the leaf-only filter because `productUpdate.category`
    accepts non-leaf GIDs end-to-end, and the leaf filter caused best-match to
    fall through to bare-token overlap and land on unrelated leaves (e.g.
    "Apparel & Accessories" → "Pager Accessories").
    """
    if not isinstance(category, str) or not category.strip():
        return None, [], "category must be a non-empty string."
    stripped = category.strip()

    if stripped.startswith(_TAXONOMY_GID_PREFIX):
        if not stripped[len(_TAXONOMY_GID_PREFIX) :]:
            return None, [], f"Empty TaxonomyCategory GID body: {stripped!r}"
        # GID passthrough — caller already knows which node they want. We don't
        # fabricate a fullName; the post-write snapshot from productUpdate fills it.
        return {"id": stripped, "fullName": None, "name": None}, [], None

    # Ordering: taxonomy search runs before the product read (on the caller
    # side) so a 0-result / ambiguous / no-exact-match failure bails CHEAP,
    # without paying for an unused product fetch. The trade-off: a transport
    # failure here happens before we've validated the product exists. The
    # try/except below makes that surface as a structured error (AC #8),
    # mirroring the product-read and mutation wrappers in the caller.
    try:
        data = client.execute(TAXONOMY_SEARCH, {"search": stripped})
    except Exception as e:
        return None, [], f"Taxonomy search failed ({type(e).__name__}): {e}"
    nodes = ((data or {}).get("taxonomy") or {}).get("categories", {}).get("nodes") or []
    candidates = list(nodes)
    if not candidates:
        return (
            None,
            [],
            f"No taxonomy categories matched search {stripped!r}. Try a broader term.",
        )

    # All three strategies do casefold comparison on the same input. Hoisting
    # the casefold call here (one place) keeps the branch bodies tight and
    # avoids two separate `needle = stripped.casefold()` definitions.
    needle = stripped.casefold()

    if resolve_strategy == "exact":
        # Match by fullName OR short name — intentional. Callers paste either
        # the node's short label ("Sweatshirts", "Clothing") or its full path
        # ("Apparel & Accessories > … > Sweatshirts"); both should resolve.
        # The >1 guard below catches the rare collision case.
        matches = [
            n
            for n in candidates
            if (n.get("fullName") or "").casefold() == needle
            or (n.get("name") or "").casefold() == needle
        ]
        if len(matches) == 0:
            return (
                None,
                [],
                f"resolve_strategy='exact' but no taxonomy category matched "
                f"{stripped!r} by fullName or name.",
            )
        if len(matches) > 1:
            return (
                None,
                [],
                f"resolve_strategy='exact' but {len(matches)} taxonomy categories "
                f"matched {stripped!r} exactly — refine the search.",
            )
        return matches[0], [], None

    if resolve_strategy == "reject-ambiguous":
        # Story 9.8 widened the candidate population (parents + intermediates
        # are no longer filtered out). The count-based rejection is preserved
        # deliberately — a query that previously succeeded because parents
        # got filtered (e.g. "Sweatshirts" returning a single leaf after
        # filtering) may now reject if Shopify also returns the parent in
        # the same response. Per Story 9.8 plan, this stricter behavior is
        # accepted as the conservative call; revisit if real callers
        # complain.
        if len(candidates) > 1:
            return (
                None,
                [],
                f"resolve_strategy='reject-ambiguous' but {len(candidates)} "
                f"taxonomy categories matched {stripped!r} — refine the search "
                f"or use resolve_strategy='best-match'.",
            )
        return candidates[0], [], None

    # best-match: 3 ordinal tiers. No `score` field — Shopify doesn't return
    # one, and synthesizing a rank-based stand-in misled downstream agents
    # into treating it as a probability. Order alone carries the signal.
    #   Tier 1 — casefold full-string equality on `name` or `fullName`. Wins
    #            outright when exactly one candidate matches (e.g. input
    #            "Apparel & Accessories" → the `aa` parent over Pager
    #            Accessories).
    #   Tier 2 — casefold prefix on `name` or `fullName`. Wins when exactly one
    #            candidate matches (e.g. input "sweatshirt" → the "Sweatshirts"
    #            leaf over the apparel parent).
    #   Tier 3 — Shopify's relevance order (the original behavior).
    # Tiers are discrete and deterministic, not a continuous similarity score.
    tier1 = [
        n
        for n in candidates
        if (n.get("fullName") or "").casefold() == needle
        or (n.get("name") or "").casefold() == needle
    ]
    if len(tier1) == 1:
        chosen = tier1[0]
    else:
        tier2 = [
            n
            for n in candidates
            if (n.get("fullName") or "").casefold().startswith(needle)
            or (n.get("name") or "").casefold().startswith(needle)
        ]
        chosen = tier2[0] if len(tier2) == 1 else candidates[0]
    # `n["id"]` is bare (not `.get("id")`) — the TAXONOMY_SEARCH GraphQL
    # schema marks `id` as non-nullable, so every node in `candidates` has it.
    alternates = [
        {
            "id": n["id"],
            "fullName": n.get("fullName"),
            "name": n.get("name"),
        }
        for n in candidates
        if n["id"] != chosen["id"]
    ]
    return chosen, alternates, None


def _build_payload(
    *,
    ok: bool,
    product: dict[str, Any] | None,
    alternates: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    preview: bool,
) -> dict[str, Any]:
    """Construct the JSON-tail payload in the spec's documented shape."""
    return {
        "ok": ok,
        "product": product,
        "alternates": alternates,
        "errors": errors,
        "preview": preview,
    }


def _shape_product_snapshot(product_node: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a raw product node into the spec's `{id, title, category}` shape."""
    if not product_node:
        return None
    category = product_node.get("category") or None
    return {
        "id": product_node.get("id"),
        "title": product_node.get("title"),
        "category": category
        and {
            "id": category.get("id"),
            "fullName": category.get("fullName"),
            "name": category.get("name"),
        },
    }


# ---------------------------------------------------------------------------
# Helpers — Story 9.2 (update_product_vendor)
# ---------------------------------------------------------------------------


def _resolve_product_id(client: ShopifyClient, product_id: str) -> tuple[str | None, dict]:
    """Resolve a numeric/GID/handle product_id to (product_gid, product_snapshot).

    Returns (None, {}) if no product was found for a handle/numeric/GID lookup
    so callers can produce a clean "not found" error. Raises ValueError on
    obvious-garbage inputs (empty string, empty GID body) so the caller's
    try/except wrapper can surface them as a structured error WITHOUT issuing
    a wasted Shopify call — mirrors Story 9.1's `_resolve_product_gid` guards.
    """
    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("product_id must be a non-empty string")
    stripped = product_id.strip()

    if stripped.startswith(_PRODUCT_GID_PREFIX):
        if not stripped[len(_PRODUCT_GID_PREFIX) :]:
            raise ValueError(f"Empty product GID body: {stripped!r}")
        gid = stripped
    elif stripped.isdigit():
        gid = to_gid("Product", stripped)
    else:
        # Handle path — separate query.
        data = client.execute(GET_PRODUCT_VENDOR_BY_HANDLE, {"handle": stripped})
        product = (data or {}).get("productByHandle") or {}
        if not product:
            return None, {}
        return product.get("id"), product

    # Numeric / GID path shares the same query.
    data = client.execute(GET_PRODUCT_VENDOR, {"id": gid})
    product = (data or {}).get("product") or {}
    if not product:
        return None, {}
    return product.get("id") or gid, product


def _normalize_vendor(vendor: str | None) -> tuple[str | None, str | None]:
    """Validate + normalize a vendor input.

    Returns `(normalized_vendor, error_message)`:
      - normalized_vendor is None when the caller wants to clear the vendor
        (vendor=None) — the mutation should send `vendor: null`.
      - normalized_vendor is a trimmed non-empty string on the happy path.
      - error_message is non-None when input was empty/whitespace/too-long;
        normalized_vendor is then meaningless and callers must short-circuit.
    """
    if vendor is None:
        return None, None
    trimmed = vendor.strip()
    if not trimmed:
        return None, "Error: vendor must be a non-empty string (use vendor=None to clear)."
    if len(trimmed) > VENDOR_MAX_LEN:
        return None, f"Error: vendor exceeds {VENDOR_MAX_LEN}-char limit (got {len(trimmed)})."
    return trimmed, None


def _format_vendor_payload(
    product_gid: str,
    vendor: str | None,
    *,
    ok: bool,
    preview: bool,
    errors: list,
) -> str:
    """Serialize the JSON tail block for update_product_vendor.

    Distinct from 9.3's `_render` (head + tail) and 9.1's `_format_payload`
    (different signature) — this helper returns ONLY the fenced JSON block,
    leaving the caller to compose the human-readable head. The dedicated
    name dodges the collision with 9.1's `_format_payload` now that both
    tools share `tools/catalog_hygiene.py`.
    """
    payload = {
        "ok": ok,
        "product": {"id": product_gid, "vendor": vendor},
        "errors": errors,
        "preview": preview,
    }
    return "```json\n" + json.dumps(payload) + "\n```"


def _vendor_text(vendor: str | None) -> str:
    """Human-readable vendor display: None or empty/whitespace → '(cleared)'."""
    return "(cleared)" if not (vendor and vendor.strip()) else vendor


def _resolve_product_id_for_type(client: ShopifyClient, product_id: str) -> tuple[str | None, dict]:
    """Resolve a numeric/GID/handle product_id to (product_gid, product_snapshot).

    Twin of `_resolve_product_id` but queries the productType field instead of
    vendor. Kept separate (rather than parameterizing the vendor helper) so the
    already-shipped Story 9.2 code path stays untouched.
    """
    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("product_id must be a non-empty string")
    stripped = product_id.strip()

    if stripped.startswith(_PRODUCT_GID_PREFIX):
        if not stripped[len(_PRODUCT_GID_PREFIX) :]:
            raise ValueError(f"Empty product GID body: {stripped!r}")
        gid = stripped
    elif stripped.isdigit():
        gid = to_gid("Product", stripped)
    else:
        # Handle path — separate query.
        data = client.execute(GET_PRODUCT_TYPE_BY_HANDLE, {"handle": stripped})
        product = (data or {}).get("productByHandle") or {}
        if not product:
            return None, {}
        return product.get("id"), product

    # Numeric / GID path shares the same query.
    data = client.execute(GET_PRODUCT_TYPE, {"id": gid})
    product = (data or {}).get("product") or {}
    if not product:
        return None, {}
    return product.get("id") or gid, product


def _normalize_product_type(product_type: str | None) -> tuple[str, str | None]:
    """Validate + normalize a productType input.

    Returns `(normalized, error_message)`. Unlike vendor:
      - empty / whitespace input is VALID — it clears the field. The
        normalized value is "" (not None) so the mutation wire-form sends
        `productType: ""` per the spec.
      - None is rejected — productType is a required field per AC #2; this
        guard is defense-in-depth for callers that bypass the type hint.
      - Length > 255 → error.
    """
    if product_type is None:
        return "", "Error: product_type is required (pass '' to clear the field)."
    trimmed = product_type.strip()
    if len(trimmed) > PRODUCT_TYPE_MAX_LEN:
        return (
            "",
            f"Error: product_type exceeds {PRODUCT_TYPE_MAX_LEN}-char limit (got {len(trimmed)}).",
        )
    return trimmed, None


def _format_type_payload(
    product_gid: str,
    product_type: str,
    *,
    ok: bool,
    preview: bool,
    errors: list,
) -> str:
    """Serialize the JSON tail block for update_product_type.

    Twin of `_format_vendor_payload`; differs only in the `product` shape
    (`productType` instead of `vendor`).
    """
    payload = {
        "ok": ok,
        "product": {"id": product_gid, "productType": product_type},
        "errors": errors,
        "preview": preview,
    }
    return "```json\n" + json.dumps(payload) + "\n```"


def _type_text(product_type: str) -> str:
    """Human-readable productType display: empty/whitespace → '(cleared)'."""
    return "(cleared)" if not product_type.strip() else product_type


# ---------------------------------------------------------------------------
# Helpers — Story 9.5 (update_product_options)
# ---------------------------------------------------------------------------


def _resolve_product_id_for_options(
    client: ShopifyClient, product_id: str
) -> tuple[str | None, dict]:
    """Resolve a numeric/GID/handle product_id to (product_gid, product_snapshot).

    Twin of `_resolve_product_id_for_type` at the productType helper above —
    same numeric/GID short-circuit, same `productByHandle` fallback for the
    handle path, but reads the options + variants slice (vs. productType).
    Returns (None, {}) when no product is found so callers emit a clean
    "not found" head without raising.
    """
    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("product_id must be a non-empty string")
    stripped = product_id.strip()

    if stripped.startswith(_PRODUCT_GID_PREFIX):
        if not stripped[len(_PRODUCT_GID_PREFIX) :]:
            raise ValueError(f"Empty product GID body: {stripped!r}")
        gid = stripped
    elif stripped.isdigit():
        gid = to_gid("Product", stripped)
    else:
        data = client.execute(GET_PRODUCT_OPTIONS_BY_HANDLE, {"handle": stripped})
        product = (data or {}).get("productByHandle") or {}
        if not product:
            return None, {}
        return product.get("id"), product

    data = client.execute(GET_PRODUCT_OPTIONS, {"id": gid})
    product = (data or {}).get("product") or {}
    if not product:
        return None, {}
    return product.get("id") or gid, product


def _normalize_option_input(
    option: object,
    option_values_to_update: object,
    variant_strategy: object,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate + normalize the caller's option / values / strategy inputs.

    Returns `(normalized, error)`:
      - On success: `(normalized, None)` where normalized is
        `{"option_id": str, "option_name": str | None, "values": list[{"id", "name"}],
          "variant_strategy": str}`. `option_name` is None when the caller didn't
        request a name change.
      - On any failure: `(None, "Error: ...")` — the caller short-circuits with
        no Shopify call. All cheap-rejects live here so the tool body deals
        with happy-path types only.

    Does NOT check GID-child-of-option (that needs the read result); only
    checks GID prefix shape, presence, types, length, and duplicate input IDs.
    """
    if not isinstance(option, dict):
        return None, "Error: option must be an object with at least an 'id' field."

    raw_id = option.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None, "Error: option.id must be a non-empty string."
    option_id = raw_id.strip()
    if not option_id.startswith(_PRODUCT_OPTION_GID_PREFIX):
        return None, (f"Error: option.id must be a ProductOption GID (got {option_id!r}).")
    if not option_id[len(_PRODUCT_OPTION_GID_PREFIX) :]:
        return None, f"Error: option.id has empty GID body: {option_id!r}."

    option_name: str | None = None
    if "name" in option:
        raw_name = option.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return None, "Error: option.name, when supplied, must be a non-empty string."
        trimmed = raw_name.strip()
        if len(trimmed) > OPTION_NAME_MAX_LEN:
            return None, (
                f"Error: option.name exceeds {OPTION_NAME_MAX_LEN}-char limit (got {len(trimmed)})."
            )
        option_name = trimmed

    # Treat None / omitted as "no value renames requested" — empty list is the
    # canonical normalized form so the empty-delta short-circuit can compare on
    # length without juggling None.
    if option_values_to_update is None:
        raw_values: list = []
    elif isinstance(option_values_to_update, list):
        raw_values = option_values_to_update
    else:
        return None, "Error: option_values_to_update must be a list (or omitted)."

    normalized_values: list[dict[str, str]] = []
    seen_value_ids: set[str] = set()
    for idx, entry in enumerate(raw_values):
        if not isinstance(entry, dict):
            return None, f"Error: option_values_to_update[{idx}] must be an object."
        raw_value_id = entry.get("id")
        if not isinstance(raw_value_id, str) or not raw_value_id.strip():
            return None, (f"Error: option_values_to_update[{idx}].id must be a non-empty string.")
        value_id = raw_value_id.strip()
        if not value_id.startswith(_PRODUCT_OPTION_VALUE_GID_PREFIX):
            return None, (
                f"Error: option_values_to_update[{idx}].id must be a "
                f"ProductOptionValue GID (got {value_id!r})."
            )
        if not value_id[len(_PRODUCT_OPTION_VALUE_GID_PREFIX) :]:
            return None, (
                f"Error: option_values_to_update[{idx}].id has empty GID body: {value_id!r}."
            )
        if value_id in seen_value_ids:
            return None, (
                f"Error: option_values_to_update[{idx}].id {value_id!r} is a "
                "duplicate; merge into a single entry."
            )
        seen_value_ids.add(value_id)

        raw_value_name = entry.get("name")
        if not isinstance(raw_value_name, str) or not raw_value_name.strip():
            return None, (f"Error: option_values_to_update[{idx}].name must be a non-empty string.")
        trimmed_name = raw_value_name.strip()
        if len(trimmed_name) > OPTION_NAME_MAX_LEN:
            return None, (
                f"Error: option_values_to_update[{idx}].name exceeds "
                f"{OPTION_NAME_MAX_LEN}-char limit (got {len(trimmed_name)})."
            )
        normalized_values.append({"id": value_id, "name": trimmed_name})

    if not isinstance(variant_strategy, str) or variant_strategy not in _VALID_VARIANT_STRATEGIES:
        return None, (
            f"Error: variant_strategy must be one of "
            f"{list(_VALID_VARIANT_STRATEGIES)} (got {variant_strategy!r})."
        )

    return (
        {
            "option_id": option_id,
            "option_name": option_name,
            "values": normalized_values,
            "variant_strategy": variant_strategy,
        },
        None,
    )


def _shape_options_snapshot(product_node: dict[str, Any] | None) -> dict[str, Any]:
    """Build the JSON-tail `product` shape from a product node.

    Returns `{id, options[{id, name, optionValues[{id, name}]}], variants[{id, title, selectedOptions[]}]}`.
    Missing top-level node yields a minimal `{"id": ""}` so the tail shape
    stays consistent across error / success paths.
    """
    if not product_node:
        return {"id": "", "options": [], "variants": []}
    options = []
    for opt in product_node.get("options") or []:
        options.append(
            {
                "id": opt.get("id"),
                "name": opt.get("name"),
                "optionValues": [
                    {"id": v.get("id"), "name": v.get("name")}
                    for v in (opt.get("optionValues") or [])
                ],
            }
        )
    variants = []
    for v in (product_node.get("variants") or {}).get("nodes") or []:
        variants.append(
            {
                "id": v.get("id"),
                "title": v.get("title"),
                "selectedOptions": [
                    {"name": so.get("name"), "value": so.get("value")}
                    for so in (v.get("selectedOptions") or [])
                ],
            }
        )
    return {
        "id": product_node.get("id") or "",
        "options": options,
        "variants": variants,
    }


def _format_options_payload(
    *,
    product_snapshot: dict[str, Any],
    ok: bool,
    preview: bool,
    errors: list,
) -> str:
    """Serialize the JSON tail block for update_product_options.

    Twin of `_format_vendor_payload` — emits ONLY the fenced JSON block so
    the caller composes the head separately. The `product` slot carries the
    full `_shape_options_snapshot` output so callers can read the post-write
    options + variants state without a follow-up get_product_full call.
    """
    payload = {
        "ok": ok,
        "product": product_snapshot,
        "errors": errors,
        "preview": preview,
    }
    return "```json\n" + json.dumps(payload) + "\n```"


def register(server: FastMCP, client: ShopifyClient) -> None:
    """Register catalog-hygiene tools on the MCP server.

    Stories 9.1-9.7 each add one `@server.tool()` function to this body.
    """

    @server.tool()
    def update_product_pricing(
        product_id: str,
        variants: list[dict[str, Any]],
        confirm: bool = False,
    ) -> str:
        """
        Update price and/or compareAtPrice on one or more variants of a
        product via productVariantsBulkUpdate. Each entry needs `variantId`
        plus at least one of `price` / `compareAtPrice`. `compareAtPrice:
        None` clears the field. `variantId` resolves from numeric / GID /
        SKU; ambiguous SKU and duplicate variantId entries fail the call.
        Decimal validation rejects the whole call if any entry is invalid
        (no partial application). Returns a preview unless confirm=True.

        Return shape: human-readable head + fenced ```json``` block with
        `{ok, variants[{id, sku, price, compareAtPrice}], errors[]}` per
        the spec.

        product_id: numeric ID or GID. Handle-based resolution is not
        supported in Story 9.3 — pass a numeric ID or GID.
        """
        # ---- Validate inputs --------------------------------------------
        try:
            entries = _normalize_entries(variants)
        except ValueError as exc:
            return _render(f"Error: {exc}", _err_payload(str(exc)))

        product_gid = to_gid("Product", product_id)

        # ---- Read existing pricing state (one round-trip) ---------------
        # Wide read pulls id+sku+price+compareAtPrice in one call. The same
        # variants list feeds both SKU resolution and the old→new preview.
        data = client.execute(GET_PRODUCT_VARIANTS_FOR_PRICING, {"id": product_gid})
        product = (data or {}).get("product")
        if not product:
            msg = f"No product found with id {product_id}."
            return _render(msg, _err_payload(msg))

        title = product.get("title", "")
        all_variants = (product.get("variants") or {}).get("nodes", []) or []
        by_gid = {v["id"]: v for v in all_variants}
        at_cap_warning = (
            f"  WARNING: variant read hit the {VARIANTS_PAGE_CAP}-variant page "
            f"cap — additional variants (if any) are not covered by this call."
            if len(all_variants) >= VARIANTS_PAGE_CAP
            else ""
        )

        # ---- Resolve variantIds in-memory (no second fetch) -------------
        try:
            resolved_gids = resolve_variant_ids_with_variants(
                [e["variantId"] for e in entries],
                all_variants,
                product_gid=product_gid,
            )
        except ValueError as exc:
            return _render(f"Error: {exc}", _err_payload(str(exc)))

        # Two different raw inputs can resolve to the same variant (e.g.,
        # "201" and "SKU-A" if SKU-A's variant is 201). _normalize_entries
        # catches identical raw inputs; this catches the post-resolution
        # collision so a redundant payload never hits Shopify.
        seen_gids: set[str] = set()
        for gid, entry in zip(resolved_gids, entries, strict=True):
            if gid in seen_gids:
                msg = (
                    f"variantId {entry['variantId']!r} resolves to the "
                    f"same variant ({from_gid(gid)}) as an earlier entry; "
                    "merge price/compareAtPrice into a single entry"
                )
                return _render(f"Error: {msg}", _err_payload(msg))
            seen_gids.add(gid)

        # ---- Build preview, detect no-op -------------------------------
        preview_lines: list[str] = []
        projected_variants: list[dict[str, Any]] = []
        not_found_messages: list[str] = []
        all_match = True
        unknown_gids: list[str] = []
        for entry, gid in zip(entries, resolved_gids, strict=True):
            existing = by_gid.get(gid)
            if existing is None:
                # Numeric/GID short-circuits resolution without consulting the
                # variants list, so a caller-supplied ID that isn't on the
                # product slips through to here. Catch it instead of letting
                # Shopify reject after the network round-trip.
                unknown_gids.append(gid)
                all_match = False
                preview_lines.append(f"    • id: {from_gid(gid)} — (variant not found on product)")
                not_found_messages.append(
                    f"variant {from_gid(gid)} not found on product {product_id}"
                )
                continue

            target_price = entry.get("price")
            has_cap = "compareAtPrice" in entry
            target_cap = entry.get("compareAtPrice") if has_cap else None

            matches = _entry_matches_existing(target_price, target_cap, has_cap, existing)
            if not matches:
                all_match = False

            sku = existing.get("sku") or "(no SKU)"
            block_lines = [f"    • id: {from_gid(gid)} — SKU: {sku}"]
            if target_price is not None:
                block_lines.append(_format_old_new("price", existing.get("price"), target_price))
            if has_cap:
                block_lines.append(
                    _format_old_new("compareAtPrice", existing.get("compareAtPrice"), target_cap)
                )
            if matches:
                block_lines.append("      (already at target)")
            preview_lines.append("\n".join(block_lines))

            projected_variants.append(_project_variant(entry, existing, gid))

        warning_block = f"\n{at_cap_warning}" if at_cap_warning else ""
        preview = (
            f"PREVIEW — Product pricing update\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Targets ({len(entries)}):\n" + "\n".join(preview_lines) + warning_block
        )

        if not confirm:
            preview_payload: dict[str, Any] = {
                "ok": not unknown_gids,
                "variants": projected_variants,
                "errors": [{"message": m} for m in not_found_messages],
            }
            return with_confirm_hint(_render(preview, preview_payload))

        # ---- Reject if any resolved gid is unknown to the product -------
        if unknown_gids:
            head = "Error: resolved variant(s) not found on product: " + ", ".join(
                from_gid(g) for g in unknown_gids
            )
            return _render(
                head,
                {
                    "ok": False,
                    "variants": [],
                    "errors": [{"message": m} for m in not_found_messages],
                },
            )

        # ---- Idempotent fast-path --------------------------------------
        if all_match:
            log_write(
                "update_product_pricing",
                f"product={product_id} variants={len(entries)} no-op",
            )
            head = (
                f"CONFIRMED — Product pricing update (no-op)\n"
                f"  Product : {title} (id: {product_id})\n"
                f"  Targets : {len(entries)} (all already at target values)"
                f"{warning_block}"
            )
            return _render(
                head,
                {"ok": True, "variants": projected_variants, "errors": []},
            )

        # ---- Build mutation input + execute ----------------------------
        # Track per-resolved-gid whether the caller passed `compareAtPrice` so
        # the post-mutation summary can distinguish "caller cleared it" from
        # "caller left it alone and Shopify's response happens to echo null".
        # Without this, an unchanged null reads as "(cleared)" in the head —
        # a copy-only bug surfaced during 9.3 integration smoke testing.
        variants_input: list[dict[str, Any]] = []
        cap_intent_by_gid: dict[str, str | None] = {}
        for entry, gid in zip(entries, resolved_gids, strict=True):
            payload: dict[str, Any] = {"id": gid}
            if "price" in entry:
                payload["price"] = entry["price"]
            if "compareAtPrice" in entry:
                payload["compareAtPrice"] = entry["compareAtPrice"]
                cap_intent_by_gid[gid] = entry["compareAtPrice"]  # value or None
            variants_input.append(payload)

        result = client.execute(
            UPDATE_PRODUCT_VARIANTS_PRICING,
            {"productId": product_gid, "variants": variants_input},
        )

        user_errors = extract_user_errors(result, "productVariantsBulkUpdate")
        if user_errors:
            # `field` is a dotted-path list on productVariantsBulkUpdate; mirror
            # the local formatter from tools/products.py:868 so paths render
            # readably (format_user_errors stringifies the whole list).
            def _fmt(e: dict[str, Any]) -> str:
                field_path = ".".join(str(f) for f in (e.get("field") or []))
                return f"{field_path or '(no field)'}: {e.get('message', '')}"

            msgs = "; ".join(_fmt(e) for e in user_errors)
            return _render(
                f"Error: {msgs}",
                {"ok": False, "variants": [], "errors": list(user_errors)},
            )

        updated = (result.get("productVariantsBulkUpdate") or {}).get("productVariants") or []

        def _cap_display(variant: dict[str, Any]) -> str:
            """Render compareAtPrice for the post-confirm head.

            Keyed off caller intent (NOT response state) so the head describes
            the call, not the variant's post-mutation state:
              - caller omitted, current is None    → "(unchanged)"
              - caller omitted, current is a value → "<current> (unchanged)"
                                                     (show state, label intent
                                                     so the reader knows the
                                                     value wasn't touched)
              - caller passed None                 → "(cleared)"
              - caller passed a value              → "<value>" (echo intent)
            """
            gid = variant.get("id")
            if gid not in cap_intent_by_gid:
                current = variant.get("compareAtPrice")
                if current is None:
                    return "(unchanged)"
                return f"{current} (unchanged)"
            intended = cap_intent_by_gid[gid]
            if intended is None:
                return "(cleared)"
            return str(intended)

        updated_lines = (
            "\n".join(
                f"    • id: {from_gid(v['id'])} — SKU: {v.get('sku') or '(no SKU)'} — "
                f"price: {v.get('price')} — compareAtPrice: {_cap_display(v)}"
                for v in updated
            )
            or "    (none returned)"
        )

        log_write(
            "update_product_pricing",
            f"product={product_id} variants={len(entries)}",
        )
        head = (
            f"CONFIRMED — Product pricing update\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Updated ({len(updated)}):\n{updated_lines}"
            f"{warning_block}"
        )
        return _render(head, {"ok": True, "variants": list(updated), "errors": []})

    @server.tool()
    def update_product_category(
        product_id: str,
        category: str,
        resolve_strategy: str = "best-match",
        confirm: bool = False,
    ) -> str:
        """
        Set or change a product's Standard Product Taxonomy category.

        `product_id` accepts numeric ID, GID, or handle.
        `category` accepts a TaxonomyCategory GID or a free-text search string.
        `resolve_strategy` ∈ {"exact", "best-match", "reject-ambiguous"} — only
        meaningful when `category` is a search string; ignored for GID inputs.

        Returns a preview unless confirm=True. On success, the output is a
        human-readable summary followed by a fenced ```json ...``` block
        carrying the spec's structured return shape (ok, product, alternates,
        errors, preview).
        """
        # --- Validate resolve_strategy up front: cheaper than after a fetch.
        if resolve_strategy not in _VALID_RESOLVE_STRATEGIES:
            payload = _build_payload(
                ok=False,
                product=None,
                alternates=[],
                errors=[
                    {
                        "message": (
                            f"Invalid resolve_strategy {resolve_strategy!r}. "
                            f"Must be one of: {', '.join(_VALID_RESOLVE_STRATEGIES)}."
                        )
                    }
                ],
                preview=not confirm,
            )
            return _format_payload(
                f"Error — update_product_category\n"
                f"  Invalid resolve_strategy: {resolve_strategy!r}\n"
                f"  Allowed: {', '.join(_VALID_RESOLVE_STRATEGIES)}",
                payload,
                confirm_hint=False,
            )

        # --- Resolve productId → Product GID (numeric / GID / handle).
        product_gid, prod_err = _resolve_product_gid(client, product_id)
        if prod_err or not product_gid:
            return _format_payload(
                f"Error — update_product_category\n  {prod_err}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=[],
                    errors=[
                        {
                            "message": prod_err or "product resolve failed",
                            "stage": "product-resolve",
                        }
                    ],
                    preview=not confirm,
                ),
                confirm_hint=False,
            )

        # --- Resolve category (GID passthrough OR taxonomy search).
        chosen, alternates, cat_err = _resolve_taxonomy_category(client, category, resolve_strategy)
        if cat_err or not chosen:
            return _format_payload(
                f"Error — update_product_category\n  {cat_err}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=alternates,
                    errors=[
                        {
                            "message": cat_err or "category resolve failed",
                            "stage": "category-resolve",
                        }
                    ],
                    preview=not confirm,
                ),
                confirm_hint=False,
            )

        target_category_gid = chosen["id"]
        target_full_name = chosen.get("fullName")

        # --- Fetch current category for idempotency + preview context.
        try:
            current_data = client.execute(GET_PRODUCT_CATEGORY, {"id": product_gid})
        except Exception as e:
            return _format_payload(
                f"Error — update_product_category\n"
                f"  Failed to read current product ({type(e).__name__}): {e}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=alternates,
                    errors=[{"message": str(e), "stage": "product-read"}],
                    preview=not confirm,
                ),
                confirm_hint=False,
            )
        current_product = (current_data or {}).get("product") or {}
        current_category = current_product.get("category") or {}
        old_category_id = current_category.get("id")
        old_category_fullname = current_category.get("fullName")

        # --- Idempotent no-op: target already set.
        if old_category_id and old_category_id == target_category_gid:
            payload = _build_payload(
                ok=True,
                product=_shape_product_snapshot(current_product),
                alternates=alternates,
                errors=[],
                preview=False,
            )
            header = (
                f"Done. Update product category (no-op, already set)\n"
                f"  Product ID : {from_gid(product_gid)}\n"
                f"  Category   : {old_category_fullname or '(unknown)'} "
                f"({old_category_id})"
            )
            return _format_payload(header, payload, confirm_hint=False)

        # --- Build the human-readable header common to preview + done branches.
        old_block = (
            "(none)"
            if not old_category_id
            else f"{old_category_id} — {old_category_fullname or '(unknown)'}"
        )
        new_full = target_full_name or "(name resolved by Shopify on write)"
        alt_count = len(alternates)
        alt_line = (
            "Alternates : (none)"
            if alt_count == 0
            else f"Alternates : {alt_count} runner-up(s) (see JSON tail)"
        )

        header_body = (
            f"  Product ID : {from_gid(product_gid)}\n"
            f"  Old        : {old_block}\n"
            f"  New        : {new_full}\n"
            f"  Resolved   : {target_category_gid} (strategy={resolve_strategy})\n"
            f"  {alt_line}"
        )

        # --- Preview branch (confirm=False): no mutation call.
        if not confirm:
            # Synthesize the post-write product shape from current title + target
            # category so callers can inspect what *would* be written.
            preview_product = {
                "id": product_gid,
                "title": current_product.get("title"),
                "category": {
                    "id": target_category_gid,
                    "fullName": target_full_name,
                    "name": chosen.get("name"),
                },
            }
            payload = _build_payload(
                ok=True,
                product=preview_product,
                alternates=alternates,
                errors=[],
                preview=True,
            )
            return _format_payload(
                f"PREVIEW — Update product category\n{header_body}",
                payload,
                confirm_hint=True,
            )

        # --- Execute branch (confirm=True): call productUpdate.
        try:
            result = client.execute(
                UPDATE_PRODUCT_CATEGORY,
                {"product": {"id": product_gid, "category": target_category_gid}},
            )
        except Exception as e:
            return _format_payload(
                f"Error — update_product_category\n  Mutation failed ({type(e).__name__}): {e}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=alternates,
                    errors=[{"message": str(e), "stage": "product-update"}],
                    preview=False,
                ),
                confirm_hint=False,
            )

        user_err = format_user_errors(result, "productUpdate")
        if user_err:
            raw_errors = (result or {}).get("productUpdate", {}).get("userErrors") or []
            return _format_payload(
                f"Error — update_product_category\n  {user_err}",
                _build_payload(
                    ok=False,
                    product=None,
                    alternates=alternates,
                    errors=raw_errors,
                    preview=False,
                ),
                confirm_hint=False,
            )

        updated_product = (result or {}).get("productUpdate", {}).get("product") or {}
        payload = _build_payload(
            ok=True,
            product=_shape_product_snapshot(updated_product),
            alternates=alternates,
            errors=[],
            preview=False,
        )
        log_write(
            "update_product_category",
            f"id={from_gid(product_gid)} | "
            f"'{old_category_fullname or '(none)'}' → "
            f"'{(updated_product.get('category') or {}).get('fullName') or target_full_name or target_category_gid}'",
        )
        return _format_payload(
            f"Done. Update product category\n{header_body}",
            payload,
            confirm_hint=False,
        )

    @server.tool()
    def update_product_vendor(
        product_id: str,
        vendor: str | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Set or clear a product's vendor (brand).

        Args:
            product_id: Numeric ID, GID, or handle.
            vendor: New vendor name (trimmed, ≤ 255 chars). Pass None to clear.
            confirm: When False (default) returns a preview without calling
                productUpdate. When True executes the mutation.

        Returns a human-readable preview/confirmation block followed by a
        fenced ```json``` block carrying `{ok, product{id, vendor}, errors, preview}`.
        Empty / whitespace-only / >255-char vendor inputs are rejected before
        any Shopify call is issued.
        """
        new_vendor, vendor_err = _normalize_vendor(vendor)
        if vendor_err:
            return f"{vendor_err}\n\n" + _format_vendor_payload(
                product_gid="",
                vendor=None,
                ok=False,
                preview=False,
                errors=[
                    {
                        "field": "vendor",
                        "message": vendor_err.removeprefix("Error: "),
                        "stage": "validation",
                    }
                ],
            )

        try:
            product_gid, product = _resolve_product_id(client, product_id)
        except Exception as exc:
            msg = f"Error resolving product_id ({type(exc).__name__}): {exc}"
            return f"{msg}\n\n" + _format_vendor_payload(
                product_gid="",
                vendor=new_vendor,
                ok=False,
                preview=False,
                errors=[{"message": str(exc), "stage": "product-resolve"}],
            )

        if not product_gid:
            msg = f"Error: no product found for {product_id!r}."
            return f"{msg}\n\n" + _format_vendor_payload(
                product_gid="",
                vendor=new_vendor,
                ok=False,
                preview=False,
                errors=[
                    {
                        "message": msg.removeprefix("Error: "),
                        "stage": "product-resolve",
                    }
                ],
            )

        old_vendor = product.get("vendor")
        # Idempotency: treat (None vs empty-string vendor) as equivalent so a
        # clear-when-already-empty doesn't issue a no-op mutation.
        current_norm = (old_vendor or "").strip() or None
        if current_norm == new_vendor:
            text = (
                f"Done. Update product vendor (no-op, already set)\n"
                f"  Product ID : {from_gid(product_gid)}\n"
                f"  Vendor     : {_vendor_text(new_vendor)}\n"
            )
            return (
                text
                + "\n"
                + _format_vendor_payload(
                    product_gid=product_gid,
                    vendor=new_vendor,
                    ok=True,
                    preview=False,
                    errors=[],
                )
            )

        header_text = "Done." if confirm else "PREVIEW —"
        body = (
            f"{header_text} Update product vendor\n"
            f"  Product ID : {from_gid(product_gid)}\n"
            f"  Old vendor : {_vendor_text(current_norm)}\n"
            f"  New vendor : {_vendor_text(new_vendor)}\n"
        )

        if not confirm:
            text = body + "\n\nReply with confirm=True to execute.\n"
            return (
                text
                + "\n"
                + _format_vendor_payload(
                    product_gid=product_gid,
                    vendor=new_vendor,
                    ok=True,
                    preview=True,
                    errors=[],
                )
            )

        try:
            result = client.execute(
                UPDATE_PRODUCT_VENDOR,
                {"product": {"id": product_gid, "vendor": new_vendor}},
            )
        except Exception as exc:
            msg = f"Error calling productUpdate ({type(exc).__name__}): {exc}"
            return f"{msg}\n\n" + _format_vendor_payload(
                product_gid=product_gid,
                vendor=new_vendor,
                ok=False,
                preview=False,
                errors=[{"message": str(exc), "stage": "product-update"}],
            )

        vendor_user_errors = extract_user_errors(result, "productUpdate")
        if vendor_user_errors:
            err_summary = "; ".join(
                f"{e.get('field')}: {e.get('message')}" for e in vendor_user_errors
            )
            text = f"Error: productUpdate userErrors: {err_summary}\n"
            return (
                text
                + "\n"
                + _format_vendor_payload(
                    product_gid=product_gid,
                    vendor=new_vendor,
                    ok=False,
                    preview=False,
                    errors=vendor_user_errors,
                )
            )

        # Post-mutation snapshot — prefer Shopify's echoed value so a stripped /
        # null-coerced vendor flows back in the JSON tail unchanged.
        updated_vendor_product = (result.get("productUpdate") or {}).get("product") or {}
        final_vendor = (
            updated_vendor_product.get("vendor")
            if "vendor" in updated_vendor_product
            else new_vendor
        )

        log_write(
            "update_product_vendor",
            f"id={from_gid(product_gid)} | "
            f"'{_vendor_text(current_norm)}' → '{_vendor_text(final_vendor)}'",
        )

        return (
            body
            + "\n"
            + _format_vendor_payload(
                product_gid=product_gid,
                vendor=final_vendor,
                ok=True,
                preview=False,
                errors=[],
            )
        )

    @server.tool()
    def update_product_type(
        product_id: str,
        product_type: str,
        confirm: bool = False,
    ) -> str:
        """
        Set or clear a product's legacy free-text productType.

        Args:
            product_id: Numeric ID, GID, or handle.
            product_type: New productType (trimmed, ≤ 255 chars). Empty string
                or whitespace-only clears the field — Shopify treats `""` as
                cleared for productType (distinct from vendor, where `null`
                is the clear path).
            confirm: When False (default) returns a preview without calling
                productUpdate. When True executes the mutation.

        Distinct from category (Story 9.1) — both fields coexist on the
        product. Themes, Liquid templates, and smart-collection rules still
        key off productType, which is why this tool exists alongside the
        Standard Taxonomy category tool.

        Returns a human-readable preview/confirmation block followed by a
        fenced ```json``` block carrying `{ok, product{id, productType}, errors, preview}`.
        """
        new_type, type_err = _normalize_product_type(product_type)
        if type_err:
            return f"{type_err}\n\n" + _format_type_payload(
                product_gid="",
                product_type="",
                ok=False,
                preview=False,
                errors=[
                    {
                        "field": "product_type",
                        "message": type_err.removeprefix("Error: "),
                        "stage": "validation",
                    }
                ],
            )

        try:
            product_gid, product = _resolve_product_id_for_type(client, product_id)
        except Exception as exc:
            msg = f"Error resolving product_id ({type(exc).__name__}): {exc}"
            return f"{msg}\n\n" + _format_type_payload(
                product_gid="",
                product_type=new_type,
                ok=False,
                preview=False,
                errors=[{"message": str(exc), "stage": "product-resolve"}],
            )

        if not product_gid:
            msg = f"Error: no product found for {product_id!r}."
            return f"{msg}\n\n" + _format_type_payload(
                product_gid="",
                product_type=new_type,
                ok=False,
                preview=False,
                errors=[
                    {
                        "message": msg.removeprefix("Error: "),
                        "stage": "product-resolve",
                    }
                ],
            )

        # Idempotency: normalize current value the same way the input was
        # normalized (trim → "" for any falsy / whitespace state), so a clear
        # against an already-cleared field is a no-op.
        old_type = product.get("productType")
        current_norm = (old_type or "").strip()
        if current_norm == new_type:
            text = (
                f"Done. Update product type (no-op, already set)\n"
                f"  Product ID   : {from_gid(product_gid)}\n"
                f"  Product type : {_type_text(new_type)}\n"
            )
            return (
                text
                + "\n"
                + _format_type_payload(
                    product_gid=product_gid,
                    product_type=new_type,
                    ok=True,
                    preview=False,
                    errors=[],
                )
            )

        header_text = "Done." if confirm else "PREVIEW —"
        body = (
            f"{header_text} Update product type\n"
            f"  Product ID   : {from_gid(product_gid)}\n"
            f"  Old type     : {_type_text(current_norm)}\n"
            f"  New type     : {_type_text(new_type)}\n"
        )

        if not confirm:
            text = body + "\n\nReply with confirm=True to execute.\n"
            return (
                text
                + "\n"
                + _format_type_payload(
                    product_gid=product_gid,
                    product_type=new_type,
                    ok=True,
                    preview=True,
                    errors=[],
                )
            )

        try:
            result = client.execute(
                UPDATE_PRODUCT_TYPE,
                {"product": {"id": product_gid, "productType": new_type}},
            )
        except Exception as exc:
            msg = f"Error calling productUpdate ({type(exc).__name__}): {exc}"
            return f"{msg}\n\n" + _format_type_payload(
                product_gid=product_gid,
                product_type=new_type,
                ok=False,
                preview=False,
                errors=[{"message": str(exc), "stage": "product-update"}],
            )

        type_user_errors = extract_user_errors(result, "productUpdate")
        if type_user_errors:
            err_summary = "; ".join(
                f"{e.get('field')}: {e.get('message')}" for e in type_user_errors
            )
            text = f"Error: productUpdate userErrors: {err_summary}\n"
            return (
                text
                + "\n"
                + _format_type_payload(
                    product_gid=product_gid,
                    product_type=new_type,
                    ok=False,
                    preview=False,
                    errors=type_user_errors,
                )
            )

        # Post-mutation snapshot — prefer Shopify's echoed value so a
        # null-coerced productType flows back unchanged in the JSON tail.
        updated_product = (result.get("productUpdate") or {}).get("product") or {}
        final_type = (
            updated_product.get("productType") or ""
            if "productType" in updated_product
            else new_type
        )

        log_write(
            "update_product_type",
            f"id={from_gid(product_gid)} | "
            f"'{_type_text(current_norm)}' → '{_type_text(final_type)}'",
        )

        return (
            body
            + "\n"
            + _format_type_payload(
                product_gid=product_gid,
                product_type=final_type,
                ok=True,
                preview=False,
                errors=[],
            )
        )

    @server.tool()
    def update_variant_image_binding(
        product_id: str,
        variant_media: list[dict[str, Any]] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Bind existing product media to one or more product variants.

        product_id  : numeric ID or GID of the product (handle not supported in v1).
        variant_media: non-empty list of entries. Each entry is a dict with:
            - variantId : str — numeric ID, ProductVariant GID, or SKU on this product
            - mediaIds  : list[str] — non-empty list of MediaImage / Video / Model3d GIDs
                          already attached to this product
        confirm     : if False (default) returns a preview; if True applies the change.

        Fetches the product's media + per-variant bound media in one query,
        rejects media GIDs that don't belong to this product, treats re-binding
        of already-bound media as idempotent success (no-op), and appends only
        the net-new media via productVariantAppendMedia. Two entries for the
        same resolved variant are merged into one mutation entry.

        Returns the dual head + ```json``` tail per the spec amendment —
        `{ok, variants[{id, sku, media[]}], errors[]}` on success;
        `{ok: false, variants: [], errors: [{message}]}` on validation or
        resolver errors; Shopify userErrors pass through verbatim.
        """
        # Step 1 — input validation (no network)
        gid = _as_product_gid(product_id)
        if not gid:
            msg = "provide product_id (numeric ID or Product GID)."
            return _render(f"Error: {msg}", _err_payload(msg))

        if not variant_media:
            msg = "variant_media must be a non-empty list."
            return _render(f"Error: {msg}", _err_payload(msg))

        normalized: list[tuple[str, list[str]]] = []
        for idx, entry in enumerate(variant_media):
            if not isinstance(entry, dict):
                msg = f"variant_media[{idx}] must be an object."
                return _render(f"Error: {msg}", _err_payload(msg))
            raw_variant_id = entry.get("variantId")
            if not isinstance(raw_variant_id, str) or not raw_variant_id.strip():
                msg = f"variant_media[{idx}].variantId must be a non-empty string."
                return _render(f"Error: {msg}", _err_payload(msg))
            raw_media_ids = entry.get("mediaIds")
            if not isinstance(raw_media_ids, list) or not raw_media_ids:
                msg = f"variant_media[{idx}].mediaIds must be a non-empty list."
                return _render(f"Error: {msg}", _err_payload(msg))
            for mi, mid in enumerate(raw_media_ids):
                if not isinstance(mid, str) or not mid.startswith("gid://shopify/"):
                    msg = (
                        f"variant_media[{idx}].mediaIds[{mi}] must be a Shopify "
                        f"media GID (got {mid!r})."
                    )
                    return _render(f"Error: {msg}", _err_payload(msg))
            normalized.append((raw_variant_id.strip(), list(raw_media_ids)))

        # Step 2 — fetch product media + per-variant bound media. The combined
        # query already pulls every variant's id + sku, so we feed that list
        # straight into Story 9.3's `resolve_variant_ids_with_variants` enabler
        # (in-memory SKU lookup) instead of paying for a second variants fetch.
        # Collapses worst-case SKU-input round-trips from 3 (resolver + combined
        # + mutation) to 2 (combined + mutation).
        data = client.execute(GET_PRODUCT_MEDIA_AND_VARIANT_MEDIA, {"id": gid})
        product = (data or {}).get("product")
        if not product:
            msg = f"No product found with id {product_id}."
            return _render(msg, _err_payload(msg))

        title = product.get("title", "")
        media_nodes = (product.get("media") or {}).get("nodes", []) or []
        product_media_set = {n.get("id") for n in media_nodes if n.get("id")}
        product_media_index = {n["id"]: n for n in media_nodes if n.get("id")}

        variant_nodes = (product.get("variants") or {}).get("nodes", []) or []
        variant_media_map: dict[str, set[str]] = {}
        variant_sku_map: dict[str, str] = {}
        for v in variant_nodes:
            vid = v.get("id")
            if not vid:
                continue
            variant_sku_map[vid] = v.get("sku") or ""
            bound = (v.get("media") or {}).get("nodes", []) or []
            variant_media_map[vid] = {m.get("id") for m in bound if m.get("id")}

        # Step 3 — resolve variant IDs in-memory against the pre-fetched list
        try:
            resolved_variant_gids = resolve_variant_ids_with_variants(
                [v for v, _ in normalized],
                variant_nodes,
                product_gid=gid,
            )
        except ValueError as exc:
            return _render(f"Error: {exc}", _err_payload(str(exc)))

        # Step 4 — validate every requested media GID belongs to this product
        unknown: list[str] = []
        for _, media_ids in normalized:
            for mid in media_ids:
                if mid not in product_media_set and mid not in unknown:
                    unknown.append(mid)
        if unknown:
            msg = f"media GIDs not on product {product_id}: {', '.join(unknown)}"
            return _render(f"Error: {msg}", _err_payload(msg))

        # Step 5a — collapse duplicate variantIds: same resolved variant appearing
        # in multiple input entries merges its mediaIds (preserves first-seen order).
        merged: dict[str, list[str]] = {}
        for resolved_gid, (_, media_ids) in zip(resolved_variant_gids, normalized, strict=True):
            bucket = merged.setdefault(resolved_gid, [])
            for mid in media_ids:
                if mid not in bucket:
                    bucket.append(mid)

        # Step 5b — compute the delta per unique variant
        deltas: list[tuple[str, list[str]]] = []
        for resolved_gid, media_ids in merged.items():
            currently = variant_media_map.get(resolved_gid, set())
            new_ids = [mid for mid in media_ids if mid not in currently]
            deltas.append((resolved_gid, new_ids))

        all_no_op = all(not new_ids for _, new_ids in deltas)
        total_new = sum(len(new_ids) for _, new_ids in deltas)

        def _variant_block(rgid: str, label_ids: list[str], label: str) -> str:
            sku = variant_sku_map.get(rgid, "")
            sku_part = f" — SKU: {sku}" if sku else ""
            lines = [f"        - {mid}" for mid in label_ids] or ["        (none)"]
            return f"    • id: {from_gid(rgid)}{sku_part}\n      {label}:\n" + "\n".join(lines)

        def _fallback_media_nodes(rgid: str) -> list[dict[str, Any]]:
            return [
                product_media_index.get(mid) or {"id": mid}
                for mid in sorted(variant_media_map.get(rgid, set()))
            ]

        def _variant_success_payload(
            rgid: str, media_nodes_for_variant: list[dict[str, Any]]
        ) -> dict[str, Any]:
            """Shared shape for no-op and post-mutation JSON-tail variants.

            Sorts media by `id` so consumers see deterministic ordering
            regardless of whether the data came from Shopify's mutation
            response or the pre-fetch fallback.
            """
            return {
                "id": rgid,
                "sku": variant_sku_map.get(rgid, ""),
                "media": sorted(
                    (_media_node_to_json(m) for m in media_nodes_for_variant),
                    key=lambda x: x.get("id") or "",
                ),
            }

        # Step 6 — preview body (reused verbatim for preview + confirmed branches)
        preview_blocks = [
            _variant_block(rgid, new_ids, "Will append" if new_ids else "Already bound")
            for rgid, new_ids in deltas
        ]
        preview_head = (
            f"PREVIEW — Bind variant images\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Variants ({len(deltas)}) — net-new media bindings: {total_new}\n"
            + "\n".join(preview_blocks)
        )

        # Step 7 — preview branch (confirm=False).
        if not confirm:
            preview_payload: dict[str, Any] = {
                "ok": True,
                "dryRun": True,
                "variants": [
                    {
                        "id": rgid,
                        "sku": variant_sku_map.get(rgid, ""),
                        "currentMedia": sorted(variant_media_map.get(rgid, set())),
                        "wouldAppend": list(new_ids),
                    }
                    for rgid, new_ids in deltas
                ],
                "errors": [],
            }
            return with_confirm_hint(_render(preview_head, preview_payload))

        # Step 8 — idempotent no-op short-circuit (no mutation call).
        if all_no_op:
            log_write(
                "update_variant_image_binding",
                f"product={product_id} variants={len(deltas)} media_bound=0 (idempotent)",
            )
            head = (
                f"CONFIRMED — Bind variant images (no-op)\n"
                f"  Product : {title} (id: {product_id})\n"
                f"  Variants ({len(deltas)}) — all requested media already bound."
            )
            return _render(
                head,
                {
                    "ok": True,
                    "variants": [
                        _variant_success_payload(rgid, _fallback_media_nodes(rgid))
                        for rgid, _ in deltas
                    ],
                    "errors": [],
                },
            )

        # Step 9 — execute the mutation for variants with non-empty deltas only
        mutation_input = [
            {"variantId": rgid, "mediaIds": new_ids} for rgid, new_ids in deltas if new_ids
        ]
        result = client.execute(
            PRODUCT_VARIANT_APPEND_MEDIA,
            {"productId": gid, "variantMedia": mutation_input},
        )

        # Drop race-condition "already bound" errors per AC #6 defensive fallback.
        raw_errors = extract_user_errors(result, "productVariantAppendMedia")
        real_errors = [e for e in raw_errors if not _is_already_bound_error(e.get("message") or "")]
        if real_errors:
            # Mirror Story 9.3's userError formatter — Shopify returns `field` as a
            # dotted-path list, so str() of the whole list reads poorly.
            def _fmt(e: dict[str, Any]) -> str:
                field_path = ".".join(str(f) for f in (e.get("field") or []))
                return f"{field_path or '(no field)'}: {e.get('message', '')}"

            msgs = "; ".join(_fmt(e) for e in real_errors)
            return _render(
                f"Error: {msgs}",
                {"ok": False, "variants": [], "errors": list(real_errors)},
            )

        # Step 10 — merge mutation response with pre-fetch state for the JSON tail.
        payload = result.get("productVariantAppendMedia") or {}
        returned_variants = payload.get("productVariants") or []
        post_state: dict[str, list[dict[str, Any]]] = {}
        for v in returned_variants:
            vid = v.get("id")
            if not vid:
                continue
            post_state[vid] = (v.get("media") or {}).get("nodes", []) or []

        success_variants = [
            _variant_success_payload(
                rgid,
                post_state[rgid] if rgid in post_state else _fallback_media_nodes(rgid),
            )
            for rgid, _ in deltas
        ]

        log_write(
            "update_variant_image_binding",
            f"product={product_id} variants={len(deltas)} media_bound={total_new}",
        )
        head = (
            f"CONFIRMED — Bind variant images\n"
            f"  Product : {title} (id: {product_id})\n"
            f"  Variants ({len(deltas)}) — net-new media bindings: {total_new}\n"
            + "\n".join(preview_blocks)
        )
        return _render(
            head,
            {"ok": True, "variants": success_variants, "errors": []},
        )

    @server.tool()
    def set_product_metafields(
        metafields: list[dict[str, Any]] | None = None,
        confirm: bool = False,
    ) -> str:
        """
        Set or update one or more metafields on Products or ProductVariants.

        metafields : non-empty list of up to 25 entries. Each entry is a dict:
            - ownerId   : str — Product or ProductVariant GID
                          (`gid://shopify/Product/...` or
                          `gid://shopify/ProductVariant/...`)
            - namespace : str — e.g. 'custom'; `app--*` is reserved and rejected
            - key       : str — e.g. 'fabric_weight_oz'
            - value     : str — value as a string. For typed values supply the
                          JSON-serialized form: '14' (number_integer),
                          'true' (boolean), '["Cold wash","Hang dry"]'
                          (list.single_line_text_field).
            - type      : str — Shopify metafield type identifier.
        confirm    : if False (default) returns a preview; if True applies
                     the change via `metafieldsSet`.

        Validation runs entirely client-side before any network call: top-level
        size (>=1, <=25), per-entry required keys, ownerId GID shape, reserved
        namespace, and a basic shape check for known types
        (number_integer, number_decimal, boolean, json, list.*). Unknown types
        pass through to Shopify validation. Errors are surfaced both in the
        human-readable head and as `errorsByIndex` in the JSON tail so callers
        can pinpoint which entries failed.

        Idempotency relies on Shopify's own `metafieldsSet` semantics —
        re-running with identical inputs returns the same metafield IDs and
        is treated as success. No client-side pre-fetch.

        On Shopify `ACCESS_DENIED` (scope-block) the response includes a
        `remediation` field listing the currently granted scopes and the
        action needed to unblock (add `write_metafields` to the OAuth grant).

        Returns the dual head + ```json``` tail —
        `{ok, metafields[{id, namespace, key, value, type, ownerType}],
        errors[], preview, errorsByIndex?, remediation?}`.
        """
        # Top-level shape gate — keeps the per-entry validator dealing with
        # `dict | non-dict` only, not `list | None | str`.
        if not isinstance(metafields, list) or not metafields:
            msg = "metafields must be a non-empty list (up to 25 entries)."
            return _render(
                f"Error: {msg}",
                _err_payload(msg, key="metafields"),
            )
        if len(metafields) > METAFIELDS_SET_MAX:
            msg = (
                f"metafields exceeds the {METAFIELDS_SET_MAX}-entry "
                f"per-call cap (got {len(metafields)})."
            )
            return _render(
                f"Error: {msg}",
                _err_payload(msg, key="metafields"),
            )

        normalized, errors_by_index = _normalize_metafield_entries(metafields)
        if normalized is None:
            # Sort indices ASC for deterministic human-readable head ordering.
            sorted_pairs = sorted(errors_by_index.items())
            lines = [f"  [{idx}] " + "; ".join(msgs) for idx, msgs in sorted_pairs]
            head = "Error: metafields validation failed\n" + "\n".join(lines)
            # Normalize to the same {"field", "message", "code"} shape used by
            # Shopify userErrors so callers always see a consistent errors[] schema.
            errors_for_payload = [
                {"field": ["metafields", str(idx)], "message": m, "code": "INVALID_INPUT"}
                for idx, msgs in sorted_pairs
                for m in msgs
            ]
            return (
                head
                + "\n\n"
                + _format_metafields_payload(
                    metafields=[],
                    errors=errors_for_payload,
                    ok=False,
                    preview=False,
                    errors_by_index={
                        str(idx): [
                            {
                                "field": ["metafields", str(idx)],
                                "message": m,
                                "code": "INVALID_INPUT",
                            }
                            for m in msgs
                        ]
                        for idx, msgs in sorted_pairs
                    },
                )
            )

        # Per-entry preview line — same shape used on preview and post-mutation
        # head, so the caller sees the same display regardless of dry-run state.
        def _entry_line(idx: int, entry: dict[str, Any]) -> str:
            return (
                f"  [{idx}] {entry['ownerType']} {entry['ownerId']} | "
                f"{entry['namespace']}.{entry['key']} = {entry['value']!r} "
                f"({entry['type']})"
            )

        entry_lines = [_entry_line(i, e) for i, e in enumerate(normalized)]

        if not confirm:
            preview_head = (
                f"PREVIEW — Set product metafields\n"
                f"  Entries ({len(normalized)}):\n" + "\n".join(entry_lines)
            )
            # Preview metafields[] echoes the normalized input (ownerId,
            # namespace, key, value, type, ownerType) — no `id` yet because
            # no mutation ran. Mirrors Story 9.6's preview/success shape
            # divergence (preview = inputs; success = Shopify-echoed).
            return with_confirm_hint(
                preview_head
                + "\n\n"
                + _format_metafields_payload(
                    metafields=[dict(e) for e in normalized],
                    errors=[],
                    ok=True,
                    preview=True,
                )
            )

        # Strip the internal `ownerType` before the mutation call — Shopify
        # infers it from `ownerId`, and `MetafieldsSetInput` doesn't accept it.
        mutation_input = []
        for e in normalized:
            row = e.copy()
            del row["ownerType"]
            mutation_input.append(row)

        try:
            result = client.execute(
                METAFIELDS_SET_MUTATION,
                {"metafields": mutation_input},
            )
        except Exception as exc:
            msg = f"Error calling metafieldsSet ({type(exc).__name__}): {exc}"
            return _render(msg, _err_payload(str(exc), key="metafields"))

        user_errors = extract_user_errors(result, "metafieldsSet")

        # ACCESS_DENIED is a class-of-error signal (scope block) per AC #10 —
        # surface remediation + granted-scope context once even if multiple
        # entries returned the same code.
        access_denied = [e for e in user_errors if e.get("code") == "ACCESS_DENIED"]
        if access_denied:
            remediation = (
                "Add 'write_metafields' scope to the OAuth grant. "
                f"Currently granted: {GRANTED_SCOPES_HINT}. "
                "See https://shopify.dev/docs/api/usage/access-scopes"
                "#authenticated-access-scopes"
            )
            err_summary = "; ".join(
                f"{'.'.join(str(f) for f in (e.get('field') or [])) or '(no field)'}: "
                f"{e.get('message', '')}"
                for e in access_denied
            )
            head = (
                f"Error: metafieldsSet ACCESS_DENIED — likely missing the "
                f"write_metafields scope.\n  {err_summary}\n"
                f"  Remediation: {remediation}"
            )
            return (
                head
                + "\n\n"
                + _format_metafields_payload(
                    metafields=[],
                    errors=list(user_errors),
                    ok=False,
                    preview=False,
                    remediation=remediation,
                )
            )

        if user_errors:
            # Mirror Story 9.6's dotted-path formatter — Shopify returns
            # `field` as a list like ["metafields", "0", "value"]; str() of
            # the raw list reads poorly in a head.
            def _fmt(e: dict[str, Any]) -> str:
                field_path = ".".join(str(f) for f in (e.get("field") or []))
                return f"{field_path or '(no field)'}: {e.get('message', '')}"

            # Bucket userErrors by entry index for the `errorsByIndex` map.
            by_index: dict[str, list[dict[str, Any]]] = {}
            for e in user_errors:
                field = e.get("field") or []
                # MetafieldsSetInput field paths look like
                # ["metafields", "<idx>", "<attr>"]. If we can't read an
                # index, bucket under "_" so it still appears in the map.
                # Shopify's schema types `field` as `[String!]!`, so
                # `isinstance(field[1], str)` is always true in practice;
                # the check guards defensively against future API changes.
                idx_str = "_"
                if len(field) >= 2 and isinstance(field[1], str) and field[1].isdigit():
                    idx_str = field[1]
                by_index.setdefault(idx_str, []).append(dict(e))

            msgs = "; ".join(_fmt(e) for e in user_errors)
            head = f"Error: metafieldsSet userErrors: {msgs}"
            return (
                head
                + "\n\n"
                + _format_metafields_payload(
                    metafields=[],
                    errors=list(user_errors),
                    ok=False,
                    preview=False,
                    errors_by_index=by_index,
                )
            )

        # Success — build response from Shopify's echoed metafields[] (preserves
        # documented key order id, namespace, key, value, type, ownerType).
        returned = (result.get("metafieldsSet") or {}).get("metafields") or []
        success_metafields = [
            {
                "id": m.get("id"),
                "namespace": m.get("namespace"),
                "key": m.get("key"),
                "value": m.get("value"),
                "type": m.get("type"),
                "ownerType": m.get("ownerType"),
            }
            for m in returned
        ]

        log_write(
            "set_product_metafields",
            f"entries={len(normalized)} owners="
            + ",".join(sorted({e["ownerType"] for e in normalized})),
        )

        head = f"CONFIRMED — Set product metafields\n  Entries ({len(normalized)}):\n" + "\n".join(
            entry_lines
        )
        return (
            head
            + "\n\n"
            + _format_metafields_payload(
                metafields=success_metafields,
                errors=[],
                ok=True,
                preview=False,
            )
        )

    @server.tool()
    def update_product_options(
        product_id: str,
        option: dict[str, Any],
        option_values_to_update: list[dict[str, Any]] | None = None,
        variant_strategy: str = "LEAVE_AS_IS",
        confirm: bool = False,
    ) -> str:
        """
        Rename a product's variant option name and/or its option value names.

        product_id  : numeric ID, GID, or handle.
        option      : dict — `{"id": "<ProductOption GID>", "name": "<new name>"?}`.
                      The option GID is required; `name` is optional (omit to
                      keep the current option name).
        option_values_to_update : optional list of dicts —
                      `[{"id": "<ProductOptionValue GID>", "name": "<new name>"}, ...]`.
                      Default is none — every entry must belong to the option
                      identified by `option.id`.
        variant_strategy : 'LEAVE_AS_IS' (default) keeps existing variants
                      pointing at the renamed values. 'MANAGE' lets Shopify
                      reconcile — rarely needed for renames; may unexpectedly
                      deduplicate variants on option-shape changes.
        confirm     : if False (default) returns a preview; if True applies
                      the change via `productOptionUpdate`.

        Pre-fetches the product's options + variants in one query, validates
        that `option.id` is on the product and that every `option_values_to_update`
        ID is a child of that option (tool-side reject — avoids a guaranteed
        Shopify userError). Short-circuits to a no-op when no name changes are
        requested OR every requested rename already matches the current state
        (AC #8 idempotency).

        Single-option-per-call restriction (AC #10) is enforced by signature:
        the `option` arg accepts exactly one option. For multi-option renames,
        the caller chains calls.

        Returns the dual head + ```json``` tail per the spec amendment —
        `{ok, product{id, options[{id, name, optionValues[{id, name}]}],
        variants[{id, title, selectedOptions[]}]}, errors[], preview}`.
        Shopify userErrors (including the `code` field) pass through verbatim
        on a mutation failure.
        """
        # Step 1 — cheap input validation (no network).
        normalized, val_err = _normalize_option_input(
            option, option_values_to_update, variant_strategy
        )
        if val_err or normalized is None:
            # `normalized is None` is the runtime witness for `val_err is not None`
            # — `_normalize_option_input` returns (None, str) on failure and
            # (dict, None) on success, so the two branches are mutually exclusive.
            # The combined guard satisfies mypy without an extra assert.
            err_msg = val_err or "Error: option validation failed."
            return f"{err_msg}\n\n" + _format_options_payload(
                product_snapshot=_shape_options_snapshot(None),
                ok=False,
                preview=False,
                errors=[
                    {
                        "field": "option",
                        "message": err_msg.removeprefix("Error: "),
                        "stage": "validation",
                    }
                ],
            )

        # Step 2 — resolve product_id (numeric / GID / handle) and pre-fetch
        # options + variants in the same query (handle path uses the _BY_HANDLE
        # twin; either way `product` carries the full snapshot we need).
        try:
            product_gid, product = _resolve_product_id_for_options(client, product_id)
        except Exception as exc:
            msg = f"Error resolving product_id ({type(exc).__name__}): {exc}"
            return f"{msg}\n\n" + _format_options_payload(
                product_snapshot=_shape_options_snapshot(None),
                ok=False,
                preview=False,
                errors=[{"message": str(exc), "stage": "product-resolve"}],
            )

        if not product_gid:
            msg = f"Error: no product found for {product_id!r}."
            return f"{msg}\n\n" + _format_options_payload(
                product_snapshot=_shape_options_snapshot(None),
                ok=False,
                preview=False,
                errors=[
                    {
                        "message": msg.removeprefix("Error: "),
                        "stage": "product-resolve",
                    }
                ],
            )

        # Step 3 — validate `option.id` is on this product. Index the options
        # by GID for O(1) lookup + child-of-option checks below.
        product_options = product.get("options") or []
        matching_option = next(
            (o for o in product_options if o.get("id") == normalized["option_id"]),
            None,
        )
        if matching_option is None:
            msg = f"Error: option.id {normalized['option_id']!r} is not on product {product_id!r}."
            return f"{msg}\n\n" + _format_options_payload(
                product_snapshot=_shape_options_snapshot(product),
                ok=False,
                preview=False,
                errors=[
                    {
                        "message": msg.removeprefix("Error: "),
                        "stage": "option-validation",
                    }
                ],
            )

        # Step 4 — validate every option-value GID is a child of `matching_option`.
        # The `if v.get("id")` filter is purely defensive: Shopify's schema types
        # `ProductOptionValue.id` as `ID!`, so a None / missing id is impossible
        # in practice. Kept so a future schema regression doesn't trip a KeyError
        # in the comprehension; not load-bearing for correctness.
        existing_values_by_id: dict[str, dict[str, Any]] = {
            v["id"]: v for v in (matching_option.get("optionValues") or []) if v.get("id")
        }
        unknown_value_ids = [
            v["id"] for v in normalized["values"] if v["id"] not in existing_values_by_id
        ]
        if unknown_value_ids:
            msg = (
                f"Error: option_values_to_update contains IDs not on option "
                f"{normalized['option_id']!r}: {', '.join(unknown_value_ids)}."
            )
            return f"{msg}\n\n" + _format_options_payload(
                product_snapshot=_shape_options_snapshot(product),
                ok=False,
                preview=False,
                errors=[
                    {
                        "message": msg.removeprefix("Error: "),
                        "stage": "option-value-validation",
                    }
                ],
            )

        current_option_name = matching_option.get("name") or ""

        # Step 5 — empty-delta short-circuit. Per AC notes: "Empty
        # optionValuesToUpdate[] combined with no option.name change → no-op;
        # the tool MAY short-circuit to success without calling the API."
        if normalized["option_name"] is None and not normalized["values"]:
            head = (
                f"Done. Update product options (no-op, no changes requested)\n"
                f"  Product ID : {from_gid(product_gid)}\n"
                f"  Option     : {current_option_name} [id: {normalized['option_id']}]\n"
            )
            return (
                head
                + "\n"
                + _format_options_payload(
                    product_snapshot=_shape_options_snapshot(product),
                    ok=True,
                    preview=False,
                    errors=[],
                )
            )

        # Step 6 — idempotency: every requested rename already matches.
        option_name_no_op = (
            normalized["option_name"] is None or normalized["option_name"] == current_option_name
        )
        values_no_op = all(
            (existing_values_by_id[v["id"]].get("name") or "") == v["name"]
            for v in normalized["values"]
        )
        if option_name_no_op and values_no_op:
            head = (
                f"Done. Update product options (no-op, already set)\n"
                f"  Product ID : {from_gid(product_gid)}\n"
                f"  Option     : {current_option_name} [id: {normalized['option_id']}]\n"
            )
            return (
                head
                + "\n"
                + _format_options_payload(
                    product_snapshot=_shape_options_snapshot(product),
                    ok=True,
                    preview=False,
                    errors=[],
                )
            )

        # Step 7 — build the preview body (reused verbatim for confirm branch).
        diff_lines: list[str] = []
        if (
            normalized["option_name"] is not None
            and normalized["option_name"] != current_option_name
        ):
            diff_lines.append(
                f"  Option name : {current_option_name!r} → {normalized['option_name']!r}"
            )
        for v in normalized["values"]:
            old_name = existing_values_by_id[v["id"]].get("name") or ""
            if old_name != v["name"]:
                diff_lines.append(f"  Value [{v['id']}]: {old_name!r} → {v['name']!r}")

        # Step 6 guarantees diff_lines is non-empty here: we only reach this
        # point when at least one of option_name / values is a real change. The
        # assert is a mypy invariant pin in the same shape as
        # `_normalize_metafield_entries` (Story 9.7) — stripped under `python -O`
        # so it's free at runtime in production but trips loudly in tests if a
        # future refactor breaks the Step 5 / Step 6 short-circuit ordering.
        assert diff_lines, (
            "Step 6 invariant: at least one of option_name / values must be a real change."
        )
        header_text = "CONFIRMED —" if confirm else "PREVIEW —"
        body = (
            f"{header_text} Update product options\n"
            f"  Product ID    : {from_gid(product_gid)}\n"
            f"  Option ID     : {normalized['option_id']}\n"
            f"  Strategy      : {normalized['variant_strategy']}\n" + "\n".join(diff_lines) + "\n"
        )

        # Step 8 — preview branch (confirm=False). No mutation; emit current
        # product snapshot so the caller can audit pre-state alongside the diff.
        if not confirm:
            text = body + "\nReply with confirm=True to execute.\n"
            return (
                text
                + "\n"
                + _format_options_payload(
                    product_snapshot=_shape_options_snapshot(product),
                    ok=True,
                    preview=True,
                    errors=[],
                )
            )

        # Step 9 — execute the productOptionUpdate mutation. `option.name` is
        # omitted from the input when the caller didn't request a name change
        # (`option_name is None`) OR when the requested name already matches
        # the current name. The second case is the no-op-slot optimization
        # added in the code-review pass: a caller redundantly passing the
        # current name alongside real value renames would otherwise ship a
        # redundant `name` field on every call. Server-side Shopify dedupes,
        # but stripping client-side keeps the mutation input minimal.
        option_input: dict[str, Any] = {"id": normalized["option_id"]}
        if normalized["option_name"] is not None and not option_name_no_op:
            option_input["name"] = normalized["option_name"]

        try:
            result = client.execute(
                UPDATE_PRODUCT_OPTION,
                {
                    "productId": product_gid,
                    "option": option_input,
                    "optionValuesToUpdate": normalized["values"],
                    "variantStrategy": normalized["variant_strategy"],
                },
            )
        except Exception as exc:
            msg = f"Error calling productOptionUpdate ({type(exc).__name__}): {exc}"
            return f"{msg}\n\n" + _format_options_payload(
                product_snapshot=_shape_options_snapshot(product),
                ok=False,
                preview=False,
                errors=[{"message": str(exc), "stage": "option-update"}],
            )

        user_errors = extract_user_errors(result, "productOptionUpdate")
        if user_errors:
            # Shopify returns `field` as a list (e.g. ["option", "name"]) plus
            # a `code` for option-rename rejections like DUPLICATE_OPTION_VALUE_NAME.
            # Preserve `code` in the head so the caller can string-match without
            # parsing the JSON tail.
            def _fmt(e: dict[str, Any]) -> str:
                field_path = ".".join(str(f) for f in (e.get("field") or []))
                code_part = f" [{e['code']}]" if e.get("code") else ""
                return f"{field_path or '(no field)'}{code_part}: {e.get('message', '')}"

            msgs = "; ".join(_fmt(e) for e in user_errors)
            return f"Error: productOptionUpdate userErrors: {msgs}\n\n" + _format_options_payload(
                product_snapshot=_shape_options_snapshot(product),
                ok=False,
                preview=False,
                errors=list(user_errors),
            )

        # Step 10 — success: shape the JSON tail from Shopify's echoed product
        # snapshot (includes the renamed option / values + the post-write
        # variants slice with refreshed selectedOptions).
        updated_product = (result.get("productOptionUpdate") or {}).get("product") or {}

        log_write(
            "update_product_options",
            f"id={from_gid(product_gid)} option={normalized['option_id']} "
            f"name_change={normalized['option_name'] is not None} "
            f"value_renames={len(normalized['values'])} "
            f"strategy={normalized['variant_strategy']}",
        )

        return (
            body
            + "\n"
            + _format_options_payload(
                product_snapshot=_shape_options_snapshot(updated_product),
                ok=True,
                preview=False,
                errors=[],
            )
        )
