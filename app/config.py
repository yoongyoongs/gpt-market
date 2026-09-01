from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")
    app_name: str = "A-Share Market MCP"
    environment: str = Field(default_factory=lambda: os.getenv("ENVIRONMENT", "development"))
    host: str = Field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = Field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    mcp_token: str | None = Field(default_factory=lambda: os.getenv("MCP_TOKEN") or None)
    gpt_web_secret: str | None = Field(default_factory=lambda: os.getenv("GPT_WEB_SECRET") or None)
    eastmoney_timeout: float = Field(default_factory=lambda: float(os.getenv("EASTMONEY_TIMEOUT", "5")))
    eastmoney_retries: int = Field(default_factory=lambda: int(os.getenv("EASTMONEY_RETRIES", "3")))
    eastmoney_proxy: str | None = Field(default_factory=lambda: os.getenv("EASTMONEY_PROXY") or os.getenv("HTTPS_PROXY") or None)
    tencent_timeout: float = Field(default_factory=lambda: float(os.getenv("TENCENT_TIMEOUT", "5")))
    tencent_proxy: str | None = Field(default_factory=lambda: os.getenv("TENCENT_PROXY") or os.getenv("HTTPS_PROXY") or None)
    fundamental_timeout: float = Field(default_factory=lambda: float(os.getenv("FUNDAMENTAL_TIMEOUT", "8")))
    fundamental_cache_seconds: int = Field(default_factory=lambda: int(os.getenv("FUNDAMENTAL_CACHE_SECONDS", "21600")))
    scan_concurrency: int = Field(default_factory=lambda: int(os.getenv("SCAN_CONCURRENCY", "12")))
    max_kline_concurrency: int = Field(default_factory=lambda: int(os.getenv("MAX_KLINE_CONCURRENCY", "8")))
    kline_cache_path: str = Field(default_factory=lambda: os.getenv("KLINE_CACHE_PATH", "data/kline_cache.sqlite3"))
    scan_history_path: str = Field(default_factory=lambda: os.getenv("SCAN_HISTORY_PATH", "data/scan_history"))
    v3_enabled: bool = Field(default_factory=lambda: os.getenv("V3_ENABLED", "false").lower() in {"1", "true", "yes", "on"})
    v3_database_url: str | None = Field(default_factory=lambda: os.getenv("V3_DATABASE_URL") or None)
    v3_database_echo: bool = Field(
        default_factory=lambda: os.getenv("V3_DATABASE_ECHO", "false").lower() in {"1", "true", "yes", "on"}
    )
    v3_database_pool_size: int = Field(default_factory=lambda: int(os.getenv("V3_DATABASE_POOL_SIZE", "5")), ge=1)
    v3_database_max_overflow: int = Field(
        default_factory=lambda: int(os.getenv("V3_DATABASE_MAX_OVERFLOW", "5")), ge=0
    )
    v3_api_token: str | None = Field(
        default_factory=lambda: os.getenv("V3_API_TOKEN") or os.getenv("MCP_TOKEN") or None
    )
    v3_api_principal_id: str = Field(
        default_factory=lambda: os.getenv("V3_API_PRINCIPAL_ID", "v3-operator")
    )
    v3_strategy_admin_token: str | None = Field(
        default_factory=lambda: os.getenv("V3_STRATEGY_ADMIN_TOKEN") or None
    )
    v3_strategy_admin_principal_id: str = Field(
        default_factory=lambda: os.getenv("V3_STRATEGY_ADMIN_PRINCIPAL_ID", "v3-strategy-admin")
    )
    v3_public_market_read: bool = Field(
        default_factory=lambda: os.getenv("V3_PUBLIC_MARKET_READ", "true").lower()
        in {"1", "true", "yes", "on"}
    )
    kline_refresh_trading_seconds: int = Field(default_factory=lambda: int(os.getenv("KLINE_REFRESH_TRADING_SECONDS", "300")))
    kline_refresh_closed_seconds: int = Field(default_factory=lambda: int(os.getenv("KLINE_REFRESH_CLOSED_SECONDS", "1800")))
    stale_after_seconds: int = 30
    old_after_seconds: int = 60
    unavailable_after_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
