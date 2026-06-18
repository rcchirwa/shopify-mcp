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
