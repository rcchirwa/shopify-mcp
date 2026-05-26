"""Root-logger configuration for shopify-mcp.

Called exactly once at process start by ShopifyClient.__init__() after
Settings is resolved. All output goes to stderr — stdout is the MCP
JSON-RPC channel and must stay clean.
"""

import logging
import sys

from settings import Settings

_configured: bool = False


def configure_logging(settings: Settings) -> None:
    """Configure root logger from Settings. Idempotent."""
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    level: int = getattr(logging, settings.log_level)
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)

    if settings.log_format == "json":
        from pythonjsonlogger.json import JsonFormatter  # lazy import

        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    handler.setFormatter(formatter)
    root.addHandler(handler)
