"""Offline tests for startup_warnings.startup_warnings().

Story 10.56 (SEC-22): a startup-time operational warning when
WEBHOOK_ALLOW_ANY_HOST (SEC-17 / Story 10.51's fail-closed opt-out) is active.
Approach 1 from the card: a covered leaf function returning warning strings,
kept separate from server.py (omitted from coverage) so the check itself is
unit-testable.
"""

import logging

from pydantic import SecretStr

from shopify_mcp.logging_config import configure_logging
from shopify_mcp.settings import Settings
from shopify_mcp.startup_warnings import startup_warnings


def _settings(**overrides: object) -> Settings:
    base: dict = {
        "shopify_store_url": "test.myshopify.com",
        "shopify_access_token": SecretStr("shpat_test00000000000000000000000"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_no_warning_when_allow_any_host_is_default_false():
    assert startup_warnings(_settings()) == []


def test_no_warning_when_allow_any_host_explicitly_false():
    assert startup_warnings(_settings(webhook_allow_any_host=False)) == []


def test_warns_when_allow_any_host_true():
    warnings = startup_warnings(_settings(webhook_allow_any_host=True))
    assert len(warnings) == 1


def test_warning_names_the_consequence_and_the_fix():
    [warning] = startup_warnings(_settings(webhook_allow_any_host=True))
    assert "register_webhook" in warning
    assert "https" in warning.lower()
    assert "WEBHOOK_ALLOWLIST_HOSTS" in warning
    assert "WEBHOOK_ALLOW_ANY_HOST" in warning


def test_warning_reaches_the_configured_stderr_handler(capsys):
    """AC1: the warning must reach the handler, not just be returned as a string.

    Mirrors the wiring in create_server(): configure_logging() first (so the
    handler is attached), then log each startup_warnings() message at WARNING
    through the server module's logger — the same call create_server() makes.
    """
    configure_logging(_settings())
    logger = logging.getLogger("shopify_mcp.server")
    for message in startup_warnings(_settings(webhook_allow_any_host=True)):
        logger.warning(message)

    captured = capsys.readouterr().err
    assert "WEBHOOK_ALLOW_ANY_HOST" in captured
    assert "register_webhook" in captured


def test_no_output_when_allow_any_host_false(capsys):
    """AC2: false/unset produces no warning and no placeholder line at all."""
    configure_logging(_settings())
    logger = logging.getLogger("shopify_mcp.server")
    for message in startup_warnings(_settings(webhook_allow_any_host=False)):
        logger.warning(message)

    assert capsys.readouterr().err == ""
