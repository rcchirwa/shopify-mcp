"""Bound reflected exception / user text to a maximum length.

Shared by the audit logger (tools/_log.py) and the tool error-reflection
sites (publications, inventory, media, catalog_hygiene) so no caller
re-implements slicing. Capping stops an attacker-controlled multi-KB field
from flooding the rotating audit log or leaking large upstream bodies
(signed-URL fragments, internal host detail) back into model context, while
leaving normal-length text byte-for-byte unchanged.
"""

# Reflected exception / upstream-response / user text echoed to the caller.
# 300 chars keeps enough of a genuine error to diagnose while bounding a
# multi-KB body; matches the prior inline resp.text[:300] slice it replaces.
REFLECT_MAX_LEN = 300


def cap(text: str, limit: int = REFLECT_MAX_LEN) -> str:
    """Truncate reflected text to at most `limit` chars (no-op when shorter)."""
    return text[:limit]
