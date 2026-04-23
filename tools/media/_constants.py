"""Constants shared across the media tools."""

# Shopify caps product images at 20 MB. Reject earlier than Shopify would to
# avoid uploading bytes that can't be attached.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# Budget for the download step. Large files over slow links blow through this —
# acceptable for v1, caller can retry.
_IMAGE_DOWNLOAD_TIMEOUT_S = 30

# Budget for waiting on newly-attached media to leave PROCESSING. Shopify
# processing regularly exceeds any reasonable synchronous wait; we keep the
# budget short and return PROCESSING (not an error) on timeout since the
# storefront renders PROCESSING media in most cases.
_MEDIA_PROCESSING_POLL_TIMEOUT_S = 15
_MEDIA_PROCESSING_POLL_INTERVAL_S = 2.0

# Shopify's `media` connection page cap. A product with more than this in one
# request needs pagination; emit an at-cap warning so operators see the
# truncation instead of silently missing media.
_MEDIA_PAGE_CAP = 100
