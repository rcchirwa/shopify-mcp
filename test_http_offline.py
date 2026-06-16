"""Offline tests for tools/_http.py — the shared HTTP header policy.

`tools/_http.py` is the single source of HTTP header policy (User-Agent) for
the raw-`requests` stack used by the media upload pipeline. These tests pin the
contract: `default_headers(settings)` returns the configured User-Agent and
reflects any override on `Settings.http_user_agent`.

Usage:
  cd ~/shopify-mcp
  source .venv/bin/activate
  pytest test_http_offline.py -v
"""

from pydantic import SecretStr

from settings import Settings
from tools._http import default_headers


def _settings(**overrides) -> Settings:
    base: dict = {
        "shopify_store_url": "test.myshopify.com",
        "shopify_access_token": SecretStr("shpat_test00000000000000000000000"),
    }
    base.update(overrides)
    return Settings(**base)


def test_default_headers_returns_configured_user_agent():
    s = _settings()
    assert default_headers(s) == {"User-Agent": s.http_user_agent}


def test_default_headers_reflects_overridden_user_agent():
    s = _settings(http_user_agent="custom-agent/9.9")
    assert default_headers(s)["User-Agent"] == "custom-agent/9.9"
