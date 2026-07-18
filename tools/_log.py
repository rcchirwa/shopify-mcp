"""
Write operation logger with bounded rotation.
All mutations are appended to aon_mcp_log.txt (max 10 MB x 5 files = 50 MB cap).
"""

import logging
import logging.handlers
import os
from datetime import datetime, timezone

from tools._scrub import cap

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "aon_mcp_log.txt")
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
    # tool_name is always a fixed in-code literal, so it's not sanitized.
    safe_description = description.replace("\r", "\\r").replace("\n", "\\n")
    # Cap after sanitization so the escaped tokens count toward the bound and
    # the on-disk line is what stays bounded.
    safe_description = cap(safe_description, MAX_DESC_LEN)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _get_logger().info("[%s] %s | %s", timestamp, tool_name, safe_description)
