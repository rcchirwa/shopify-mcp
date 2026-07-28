"""Root-logger configuration for shopify-mcp.

Called exactly once at process start by ShopifyClient.__init__() after
Settings is resolved. All output goes to stderr — stdout is the MCP
JSON-RPC channel and must stay clean.
"""

import logging
import sys

from shopify_mcp.settings import Settings

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

    # Clamp noisy third-party loggers that inherit from root (SEC-19 / Story
    # 10.53). The pinned gql==3.5.3 transport (gql.transport.requests /
    # gql.transport.aiohttp) logs full request payloads and response bodies
    # at INFO with no level of its own — that includes plaintext discount
    # codes and staged-upload signing material passed as GraphQL variables.
    # Without this clamp, setting root to INFO (the default) or DEBUG makes
    # every Shopify Admin API request/response body land on stderr verbatim,
    # bypassing the redaction done at client.py and the individual tools.
    # `max(level, WARNING)` holds the clamp at WARNING even when log_level is
    # DEBUG, so a debug-logging request never reopens this leak. urllib3 and
    # requests get the same treatment since they can log connection/header
    # detail that isn't meant for routine output either.
    #
    # Deliberate opt-in decision: no dedicated setting was added to re-enable
    # gql's verbose trace. No caller in this codebase needs raw wire-level
    # GraphQL tracing — client.py already logs variable *keys* (not values)
    # for debugging, which is the supported way to trace requests. If a real
    # debugging need for full payload/response tracing shows up later, add a
    # narrowly-scoped opt-in setting (e.g. `debug_gql_trace: bool = False`)
    # that is documented in .env.example/README.md as secret-bearing, rather
    # than removing this clamp.
    # "requests" covers RequestsHTTPTransport, the only transport client.py
    # constructs. If a future change adopts gql's aiohttp transport instead,
    # add "aiohttp" here too — its client logger is a separate top-level
    # namespace from "gql" and would not otherwise inherit this clamp.
    noisy_level = max(level, logging.WARNING)
    for name in ("gql", "urllib3", "requests"):
        logging.getLogger(name).setLevel(noisy_level)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)

    if settings.log_format == "json":
        from pythonjsonlogger.json import JsonFormatter  # lazy import

        formatter: logging.Formatter = JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    handler.setFormatter(formatter)
    root.addHandler(handler)
