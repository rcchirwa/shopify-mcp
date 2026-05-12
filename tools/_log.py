"""
Write operation logger.
All writes go to aon_mcp_log.txt with timestamp, tool name, and change description.
"""

import os
from datetime import datetime

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "aon_mcp_log.txt")


def log_write(tool_name: str, description: str) -> None:
    # Sanitize control characters from description before writing — a
    # user-supplied identifier containing \n or \r could otherwise forge
    # additional log lines or break line-oriented parsing of the audit log.
    # tool_name is always a fixed in-code literal, so it's not sanitized.
    safe_description = description.replace("\r", "\\r").replace("\n", "\\n")
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"[{timestamp}] {tool_name} | {safe_description}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)
