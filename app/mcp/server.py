from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, TypeVar

from fastmcp import FastMCP

from app.container import container
from app.models import ErrorResponse
from app.serialization import serialize_business
from app.utils.time import now_shanghai

T = TypeVar("T")
mcp = FastMCP("A-Share Real-time Market", instructions="Read-only real-time A-share, ETF, index and sector market facts from Eastmoney.")


async def _safe(operation: Awaitable[T]) -> Any:
    try:
        result = await operation
        return serialize_business(result)
    except Exception as exc:
        return ErrorResponse(error=str(exc), server_timestamp=now_shanghai()).model_dump(mode="json")


@mcp.tool
async def get_quote(code: str) -> dict[str, Any]:
    """Get one current A-share, ETF or index quote by its six-digit code."""
    return await _safe(container.quotes.get_quote(code))


@mcp.tool
async def get_quotes(codes: list[str]) -> Any:
    """Get up to 100 quotes concurrently; the provider is never queried serially per code."""
    return await _safe(container.quotes.get_quotes(codes))


@mcp.tool
async def get_kline(code: str, period: str = "day", limit: int = 120) -> dict[str, Any]:
    """Get adjusted OHLCV candles. Period: 1m, 5m, 15m, 30m, 60m, day, week or month."""
    return await _safe(container.klines.get_kline(code, period, limit))


@mcp.tool
async def get_stock_detail(code: str) -> dict[str, Any]:
    """Combine quote, 120 daily candles, 48 five-minute candles and basic technical indicators."""
    return await _safe(container.klines.get_stock_detail(code))


@mcp.tool
async def get_market_overview() -> dict[str, Any]:
    """Get Shanghai, Shenzhen and ChiNext indices, breadth and total A-share turnover."""
    return await _safe(container.market.get_market_overview())


@mcp.tool
async def get_sector_ranking(sector_type: str = "industry", limit: int = 30) -> dict[str, Any]:
    """Rank Eastmoney industry or concept sectors by current percentage change."""
    return await _safe(container.sectors.get_sector_ranking(sector_type, limit))


@mcp.tool
async def scan_mainboard(
    top_n: int = 30,
    max_pct_change: float = 5.0,
    min_amount: float = 50_000_000,
    exclude_st: bool = True,
    exclude_limit_up: bool = True,
    exclude_limit_down: bool = True,
) -> dict[str, Any]:
    """Scan liquid Shanghai/Shenzhen main-board stocks and favor strength before a large daily rise."""
    return await _safe(container.scanner.scan_mainboard(
        top_n, max_pct_change, min_amount, exclude_st, exclude_limit_up, exclude_limit_down
    ))


@mcp.tool
async def get_scan_coverage() -> dict[str, Any]:
    """Report quote-list coverage and freshness for the latest main-board scan."""
    return await _safe(container.scanner.get_scan_coverage())
