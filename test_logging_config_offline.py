"""Offline tests for logging_config.configure_logging().

Isolation: the _reset_root_logger autouse fixture in conftest.py resets the
_configured flag and removes our StreamHandler after every test, so each test
gets a clean slate without disturbing pytest's own LogCaptureHandler instances.
"""

import logging

import pytest
from pydantic import SecretStr

from logging_config import configure_logging
from settings import Settings


def _settings(**overrides: object) -> Settings:
    base: dict = {
        "shopify_store_url": "test.myshopify.com",
        "shopify_access_token": SecretStr("shpat_test00000000000000000000000"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _our_handlers() -> list[logging.Handler]:
    """Return only the StreamHandler(s) added by configure_logging().

    Uses type() not isinstance(): pytest's LogCaptureHandler is a StreamHandler
    subclass, so isinstance would incorrectly include pytest's own handlers.
    """
    return [h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler]


def test_text_format_uses_standard_formatter() -> None:
    from pythonjsonlogger.json import JsonFormatter

    configure_logging(_settings(log_format="text", log_level="INFO"))
    handlers = _our_handlers()
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, logging.Formatter)
    assert not isinstance(handlers[0].formatter, JsonFormatter)


def test_json_format_uses_json_formatter() -> None:
    from pythonjsonlogger.json import JsonFormatter

    configure_logging(_settings(log_format="json", log_level="INFO"))
    handlers = _our_handlers()
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, JsonFormatter)


def test_idempotent_skips_second_call() -> None:
    configure_logging(_settings())
    configure_logging(_settings())
    assert len(_our_handlers()) == 1


def test_root_logger_level_set_from_settings() -> None:
    configure_logging(_settings(log_level="WARNING"))
    assert logging.getLogger().level == logging.WARNING


def test_debug_level_emits_debug(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(_settings(log_level="DEBUG"))
    logging.getLogger("test.debug_emit").debug("debug message")
    assert "debug message" in capsys.readouterr().err


def test_warning_level_suppresses_debug(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(_settings(log_level="WARNING"))
    logging.getLogger("test.debug_suppress").debug("should not appear")
    assert "should not appear" not in capsys.readouterr().err


def test_audit_logger_propagate_not_mutated() -> None:
    audit = logging.getLogger("shopify_aon.audit")
    audit.propagate = True  # default — configure_logging must not override this
    configure_logging(_settings())
    assert audit.propagate is True
