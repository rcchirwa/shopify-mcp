"""
Write operation logger with bounded rotation.
All mutations are appended to aon_mcp_log.txt (max 10 MB x 5 files = 50 MB cap).
"""

import logging
import logging.handlers
import os
from datetime import UTC, datetime
from pathlib import Path

from shopify_mcp.tools._scrub import cap, sanitize_control_chars

# The write-audit trail belongs at the repo root, beside the code being audited
# — not inside the package. Under the flat layout that was two dirnames up from
# this module; the src/ move (Story 10.47 / FS-3) put two more directories
# between them, so this is now parents[3]:
#
#     <repo root>/src/shopify_mcp/tools/_log.py
#      parents[3]  [2]  [1]        [0]
#
# Kept a str, not a Path: tests swap LOG_FILE for a tmp path and _get_logger
# compares it by value against _current_log_file.
LOG_FILE = str(Path(__file__).resolve().parents[3] / "aon_mcp_log.txt")
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
_BACKUP_COUNT = 5  # keep 5 rotated files -> 50 MB cap total

# Central bound on a single audit-line description. Applied to every caller so
# an unbounded, attacker-controlled field (product title, discount title,
# webhook endpoint) can't churn the rotating log and evict genuine history.
# Generous enough that normal lines — including multi-variant bulk summaries —
# are unchanged, small enough that one write can't dominate a 10 MB file.
MAX_DESC_LEN = 4000

_logger: logging.Logger | None = None
_current_log_file: str | None = None  # raw LOG_FILE value when _logger was created


def _get_logger() -> logging.Logger:
    global _logger, _current_log_file
    if _logger is None or not _logger.handlers or _current_log_file != LOG_FILE:
        if _logger is not None:
            for h in _logger.handlers:
                h.close()
            _logger.handlers.clear()
        logger = logging.getLogger("shopify_aon.audit")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = logging.handlers.RotatingFileHandler(
            os.path.abspath(LOG_FILE),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        _logger = logger
        _current_log_file = LOG_FILE
    return _logger


def log_write(tool_name: str, description: str) -> None:
    # Sanitize control characters — caller-supplied identifiers containing \n/\r
    # must not forge additional log lines or break line-oriented audit parsing.
    # Both fields go through sanitize_control_chars (Story 10.58 / SEC-24,
    # L1): tool_name is a fixed in-code literal at every present call site,
    # but that fact lives in each caller, not in this function's signature —
    # a future helper that threads tool_name from a parameter would silently
    # reopen log forging with no test failing. Sanitizing here removes the
    # dependence on that external invariant instead of merely documenting it.
    safe_tool_name = sanitize_control_chars(tool_name)
    safe_description = sanitize_control_chars(description)
    # Cap after sanitization so the escaped tokens count toward the bound and
    # the on-disk line is what stays bounded. tool_name gets no length cap:
    # every caller passes a short, fixed in-code identifier (e.g.
    # "update_product_pricing"), so there is no unbounded field to bound —
    # unlike description, which can carry caller-controlled data.
    safe_description = cap(safe_description, MAX_DESC_LEN)
    timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _get_logger().info("[%s] %s | %s", timestamp, safe_tool_name, safe_description)
