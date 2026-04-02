"""
Write operation logger.
All writes go to aon_mcp_log.txt with timestamp, tool name, and change description.
"""

import os
from datetime import datetime

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "aon_mcp_log.txt")


def log_write(tool_name: str, description: str) -> None:
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"[{timestamp}] {tool_name} | {description}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)
