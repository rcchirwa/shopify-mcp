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


def sanitize_control_chars(text: str) -> str:
    """Escape CR/LF so caller-supplied text can't forge extra log lines.

    Shared by the audit logger (tools/_log.py) and any other single-line
    log/diagnostic sink (e.g. client.py's retry warning label) that embeds
    attacker-influenced text. A literal ``\\r``/``\\n`` in the input becomes
    additional log entries once written through a line-oriented formatter or
    handler — this collapses each back to a visible two-character escape
    token instead, leaving text with no control characters byte-for-byte
    unchanged.
    """
    return text.replace("\r", "\\r").replace("\n", "\\n")
