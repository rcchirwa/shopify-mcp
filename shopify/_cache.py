"""Cross-call TTL cache for stable Shopify metadata (A8 / Story 10.32).

Channels-only today. Publication channels are the one piece of stable metadata
re-resolved from the API on every MCP tool call (``tools/publications.py``), so the
``channels`` bucket is the only one wired to a real cross-call read. The cache is a
registry keyed by resource type, so adding a second resource later (locations, shop
info, metafield definitions — none of which are read anywhere yet, see the Story
10.32 scope note) is a one-line entry in ``__init__`` plus its ``Settings`` TTL field,
with no structural change.

Per-resource TTLs come from ``Settings`` (extending the A7 config pattern) rather than
hard-coded literals, and the timer is injectable so tests drive expiry with a
controllable clock instead of wall-clock sleeps.
"""

import time
from collections.abc import Callable
from typing import Any

from cachetools import TTLCache

from settings import Settings

# Resource-type key for the channels bucket. Named so the read-through and
# write-invalidation paths in tools/publications.py agree on one spelling instead
# of passing a bare "channels" string around. Keys for the deferred resources are
# intentionally absent — each gets added alongside its first real cross-call read.
CHANNELS = "channels"

# Each bucket currently caches a single snapshot (e.g. the full channels list), so
# one constant slot key per bucket suffices. A per-id resource would key by id here.
_SLOT = "_all"


class ShopifyMetadataCache:
    """Per-resource-type TTL caches for stable Shopify metadata.

    Wraps one :class:`cachetools.TTLCache` per resource type. :meth:`get` /
    :meth:`set` operate on a resource's single cached snapshot; :meth:`invalidate`
    drops it (used by write paths that may change the resource). Cached values are
    treated as read-only by callers — the cache stores the reference, not a copy.
    """

    def __init__(self, settings: Settings, *, timer: Callable[[], float] = time.monotonic) -> None:
        # One bucket per resource type, each with its Settings-driven TTL. maxsize=1
        # because every bucket holds a single snapshot under _SLOT; `timer` defaults
        # to TTLCache's own time.monotonic and is overridden by tests for expiry.
        self._buckets: dict[str, TTLCache] = {
            CHANNELS: TTLCache(maxsize=1, ttl=settings.cache_ttl_channels_s, timer=timer),
        }

    def get(self, resource: str) -> Any | None:
        """Return ``resource``'s cached snapshot, or None on a cold or expired miss."""
        return self._buckets[resource].get(_SLOT)

    def set(self, resource: str, value: Any) -> None:
        """Cache ``value`` as ``resource``'s snapshot, subject to the bucket's TTL."""
        self._buckets[resource][_SLOT] = value

    def invalidate(self, resource: str) -> None:
        """Drop ``resource``'s cached snapshot so the next read re-fetches."""
        self._buckets[resource].clear()
