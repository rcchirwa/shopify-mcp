"""Centralized config for shopify-mcp.

Pydantic-settings BaseSettings instance is built once at ShopifyClient
construction; tests can pass a custom Settings to override any field without
mutating module-level state. Validators fail fast at startup (regex on
shopify_store_url / shopify_api_version) instead of waiting for the first
GraphQL call to fail with a less obvious error.

See architectural_tech_debt.md item A7 for design rationale.
"""

import re
import sys
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_STORE_URL_RE = re.compile(r"^[a-z0-9-]+\.myshopify\.com$", re.IGNORECASE)
_API_VERSION_RE = re.compile(r"^\d{4}-\d{2}$")


class Settings(BaseSettings):
    # env_file=None: ShopifyClient calls load_dotenv(_ENV_PATH, override=True)
    # before instantiating Settings(), so we read only from process env. Letting
    # pydantic-settings also load .env would double-load with different
    # precedence rules (process env wins by default — opposite of override=True).
    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    shopify_store_url: str
    shopify_access_token: SecretStr
    shopify_api_version: str = "2026-01"

    # int (not float): gql's RequestsHTTPTransport.timeout is typed Optional[int].
    # Sub-second tuning isn't useful for HTTP request timeouts anyway.
    request_timeout_s: int = 15
    job_poll_timeout_s: float = 10.0
    retry_max_attempts: int = 5
    retry_base_s: float = 0.5
    retry_cap_s: float = 30.0
    poll_base_s: float = 0.5
    poll_cap_s: float = 5.0

    webhook_allowlist_hosts: str = ""

    # A8 (metadata TTLCache) — field exists so that item doesn't need a config
    # touch-up when it lands. log_level / log_format wired by A4 (logging).
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    cache_ttl_locations_s: int = 3600

    @field_validator("shopify_store_url")
    @classmethod
    def _validate_store_url(cls, v: str) -> str:
        if not _STORE_URL_RE.match(v):
            raise ValueError(f"SHOPIFY_STORE_URL must match '<shop>.myshopify.com' (got {v!r})")
        return v

    @field_validator("shopify_api_version")
    @classmethod
    def _validate_api_version(cls, v: str) -> str:
        if not _API_VERSION_RE.match(v):
            raise ValueError(f"SHOPIFY_API_VERSION must match YYYY-MM (got {v!r})")
        return v

    @field_validator("shopify_access_token")
    @classmethod
    def _warn_token_prefix(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().startswith("shpat_"):
            print(
                "[settings] WARN: SHOPIFY_ACCESS_TOKEN does not start with 'shpat_' — "
                "verify it is an Admin API access token.",
                file=sys.stderr,
            )
        return v

    @property
    def webhook_allowlist_set(self) -> frozenset[str]:
        raw = self.webhook_allowlist_hosts.strip()
        if not raw:
            return frozenset()
        return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())
