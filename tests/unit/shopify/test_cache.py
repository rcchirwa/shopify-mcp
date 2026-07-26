"""Offline unit tests for shopify/_cache.py — the cross-call metadata TTL cache.

Channels-only today (Story 10.32 / A8): exercises the per-resource-type registry,
the get/set/invalidate surface, and TTL expiry driven by an injected clock so the
test controls time instead of sleeping.
"""

from pydantic import SecretStr

from settings import Settings
from shopify._cache import CHANNELS, ShopifyMetadataCache


def _settings(channels_ttl: int = 600) -> Settings:
    return Settings(
        shopify_store_url="test.myshopify.com",
        shopify_access_token=SecretStr("shpat_test00000000000000000000000"),
        cache_ttl_channels_s=channels_ttl,
    )


def test_get_returns_none_on_cold_miss():
    cache = ShopifyMetadataCache(_settings())
    assert cache.get(CHANNELS) is None


def test_set_then_get_returns_cached_value():
    cache = ShopifyMetadataCache(_settings())
    cache.set(CHANNELS, ["online-store", "pos"])
    assert cache.get(CHANNELS) == ["online-store", "pos"]


def test_invalidate_drops_cached_entry():
    cache = ShopifyMetadataCache(_settings())
    cache.set(CHANNELS, ["online-store"])
    cache.invalidate(CHANNELS)
    assert cache.get(CHANNELS) is None


def test_invalidate_is_idempotent_on_cold_bucket():
    """Invalidating an already-empty bucket is a no-op, not an error — write
    paths can invalidate unconditionally without first checking for a hit."""
    cache = ShopifyMetadataCache(_settings())
    cache.invalidate(CHANNELS)  # nothing cached yet
    assert cache.get(CHANNELS) is None


def test_entry_expires_after_ttl_with_injected_clock():
    clock = {"t": 1000.0}
    cache = ShopifyMetadataCache(_settings(channels_ttl=10), timer=lambda: clock["t"])
    cache.set(CHANNELS, ["online-store"])
    clock["t"] += 9  # still within the 10s TTL window
    assert cache.get(CHANNELS) == ["online-store"]
    clock["t"] += 2  # 11s elapsed — past the TTL
    assert cache.get(CHANNELS) is None


def test_ttl_is_settings_driven_not_hardcoded():
    """A custom Settings.cache_ttl_channels_s is honored: a 5s TTL expires at
    t=6, which a hard-coded default (e.g. 600) would not."""
    clock = {"t": 0.0}
    cache = ShopifyMetadataCache(_settings(channels_ttl=5), timer=lambda: clock["t"])
    cache.set(CHANNELS, ["online-store"])
    clock["t"] = 6
    assert cache.get(CHANNELS) is None
