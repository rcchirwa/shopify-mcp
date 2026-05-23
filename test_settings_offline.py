"""Offline tests for Settings validators (A7).

Covers the failure branches of the three field validators:
  - shopify_store_url must match `<shop>.myshopify.com`
  - shopify_api_version must match YYYY-MM
  - shopify_access_token: warn-only print to stderr when prefix isn't `shpat_`
"""

import pytest
from pydantic import SecretStr, ValidationError

from settings import Settings


def _ok_kwargs(**overrides) -> dict:
    base: dict = {
        "shopify_store_url": "test.myshopify.com",
        "shopify_access_token": SecretStr("shpat_test00000000000000000000000"),
    }
    base.update(overrides)
    return base


def test_invalid_store_url_raises():
    with pytest.raises(ValidationError, match="SHOPIFY_STORE_URL must match"):
        Settings(**_ok_kwargs(shopify_store_url="not-a-shop.example.com"))


def test_invalid_api_version_raises():
    with pytest.raises(ValidationError, match="SHOPIFY_API_VERSION must match YYYY-MM"):
        Settings(**_ok_kwargs(shopify_api_version="bogus"))


def test_token_missing_shpat_prefix_warns_to_stderr(capsys):
    Settings(**_ok_kwargs(shopify_access_token=SecretStr("plain_token_no_prefix")))
    err = capsys.readouterr().err
    assert "WARN: SHOPIFY_ACCESS_TOKEN does not start with 'shpat_'" in err


def test_token_with_shpat_prefix_does_not_warn(capsys):
    Settings(**_ok_kwargs())
    assert capsys.readouterr().err == ""


def test_webhook_allowlist_set_parses_csv():
    s = Settings(**_ok_kwargs(webhook_allowlist_hosts="A.example.com, b.example.com,"))
    assert s.webhook_allowlist_set == frozenset({"a.example.com", "b.example.com"})


def test_webhook_allowlist_set_empty():
    s = Settings(**_ok_kwargs(webhook_allowlist_hosts=""))
    assert s.webhook_allowlist_set == frozenset()
