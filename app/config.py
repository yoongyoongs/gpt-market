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
    scan_concurrency: int = Field(default_factory=lambda: int(os.getenv("SCAN_CONCURRENCY", "12")))
    stale_after_seconds: int = 30
    old_after_seconds: int = 60
    unavailable_after_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
