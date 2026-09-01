from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from app.v3.contracts.base import V3Contract


DEEP_PERIODS = ("5m", "15m", "60m")
DEFAULT_SOURCE = "eastmoney"


class IntradayStructure(V3Contract):
    """Deep Market Data 输出（RC-04D / DAT-001）。

    分钟事实是抓取时点事实：只服务 Action/Watchlist/Portfolio 的即时
    深度结构，不落入全市场长期存储；重放（as_of 早于抓取时刻）时精度
    一律 LIMITED，绝不伪装为精确历史时点数据。
    """

    code: str
    as_of: datetime
    known_at: datetime
    source: str
    periods: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DeepMarketDataService:
    def __init__(
        self,
        provider: Any,
        *,
        clock: Any = None,
        source: str = DEFAULT_SOURCE,
        periods: tuple[str, ...] = DEEP_PERIODS,
        bars_per_period: int = 32,
    ) -> None:
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._source = source
        self._periods = periods
        self._bars_per_period = bars_per_period

    async def get_intraday_structure(
        self, code: str, *, as_of: datetime
    ) -> IntradayStructure:
        known_at = self._clock()
        periods: dict[str, dict[str, Any]] = {}
        for period in self._periods:
            periods[period] = await self._read_period(
                code, period, as_of=as_of
            )
        return IntradayStructure(
            code=code, as_of=as_of, known_at=known_at,
            source=self._source, periods=periods,
        )

    async def _read_period(
        self, code: str, period: str, *, as_of: datetime
    ) -> dict[str, Any]:
        try:
            result = await self._provider.get_kline(
                code, period, self._bars_per_period, adjust="raw",
            )
        except Exception as exc:
            return {
                "status": "UNKNOWN",
                "reason": f"{type(exc).__name__}: {exc}",
                "precision": "UNKNOWN",
                "bar_count": 0,
                "stale": None,
            }
        bars = [
            bar for bar in result.klines if bar.timestamp <= as_of
        ]
        if not bars:
            return {
                "status": "UNKNOWN",
                "reason": "NO_BARS_KNOWN_AT_AS_OF",
                "precision": "UNKNOWN",
                "bar_count": 0,
                "stale": result.stale,
            }
        return {
            "status": "AVAILABLE",
            "precision": "LIMITED",
            # 分钟事实不构成可重放的时点数据：显式声明精度限制
            "reason": "MINUTE_FACTS_ARE_FETCH_TIME_FACTS",
            "bar_count": len(bars),
            "first_bar_time": bars[0].timestamp,
            "last_bar_time": bars[-1].timestamp,
            "provisional": any(bar.provisional for bar in bars),
            "stale": result.stale,
        }
