"""Constants shared across the media tools."""

# Shopify caps product images at 20 MB. Reject earlier than Shopify would to
# avoid uploading bytes that can't be attached.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# The image-download timeout now lives on Settings (`download_timeout_s`),
# alongside `staged_upload_timeout_s` and `request_timeout_s` — all three HTTP
# timeouts are config-driven after Story 10.24 / A6. The download GET itself
# runs through ShopifyClient.fetch_bytes().

# Budget for waiting on newly-attached media to leave PROCESSING. Shopify
# processing regularly exceeds any reasonable synchronous wait; we keep the
# budget short and return PROCESSING (not an error) on timeout since the
# storefront renders PROCESSING media in most cases.
_MEDIA_PROCESSING_POLL_TIMEOUT_S = 15
_MEDIA_PROCESSING_POLL_INTERVAL_S = 2.0

# Page size for media pagination via `paginate()`. Shopify's `media` connection
# returns up to 250 nodes per request; 100 is a safe default.
_MEDIA_PAGE_CAP = 100

# Per-call cap on media-id list params. Same idiom as catalog_hygiene's
# METAFIELDS_SET_MAX / METAFIELDS_DELETE_MAX — enforced client-side before any
# network call. Shared by delete_product_media's `media_ids` and
# update_variant_image_binding's nested `mediaIds` (Story 10.42 / SEC-09).
MEDIA_IDS_MAX = 25

# Per-call cap on reorder_product_media's `moves` list. Same idiom as
# MEDIA_IDS_MAX (Story 10.42 / SEC-09).
MOVES_MAX = 25
