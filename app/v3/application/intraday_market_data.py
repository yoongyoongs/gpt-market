"""RT-01：Intraday Market Data Adapter（实时方案 §4.3/§27 RT-01）。

复用 Legacy MarketDataService/ProviderManager（东财/腾讯/新浪、Kline 聚合、
缓存、provisional 标记），不平行造行情 Provider：

- get_quote_snapshot：Legacy Quote → V3 IntradayQuoteSnapshot（时点三元组完整）；
- get_intraday_bars：1m/5m/15m/60m/day/week → V3 IntradayBarsResult，
  单周期故障隔离、as_of 过滤、PROVISIONAL 显式透传。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.v3.contracts.base import require_aware
from app.v3.domain.intraday import (
    IntradayBar,
    IntradayBarSeries,
    IntradayBarsResult,
    IntradayQuoteSnapshot,
)

_DEFAULT_PERIODS = ("1m", "5m", "15m", "60m", "day", "week")


class _MarketProvider(Protocol):
    async def get_quote(self, code: str) -> Any: ...
    async def get_kline(
        self, code: str, period: str, limit: int, adjust: str = "qfq"
    ) -> Any: ...


class IntradayMarketDataService:
    """V3 实时行情适配层：Legacy 事实 → V3 契约，只映射不改写。"""

    def __init__(
        self, provider: _MarketProvider, *, bars_per_period: int = 240,
    ) -> None:
        self._provider = provider
        self._bars_per_period = bars_per_period

    async def get_quote_snapshot(self, code: str, *, as_of: datetime) -> IntradayQuoteSnapshot:
        require_aware(as_of, "as_of")
        quote = await self._provider.get_quote(code)
        return IntradayQuoteSnapshot(
            code=quote.code,
            market=quote.market,
            name=quote.name,
            last_price=quote.price,
            open=quote.open,
            high=quote.high,
            low=quote.low,
            prev_close=quote.prev_close,
            change=quote.change,
            change_pct=quote.pct_change,
            volume=quote.volume,
            amount=quote.amount,
            turnover_rate=quote.turnover_rate,
            volume_ratio=quote.volume_ratio,
            suspended=quote.suspended,
            event_time=quote.data_timestamp,
            fetch_time=quote.server_timestamp,
            known_at=quote.server_timestamp,
            as_of=as_of,
            source=quote.source,
            upstream_source=quote.timestamp_source,
            quality=quote.quality,
            stale=quote.stale,
            confidence=quote.confidence,
        )

    async def get_intraday_bars(
        self,
        code: str,
        periods: tuple[str, ...] = _DEFAULT_PERIODS,
        *,
        as_of: datetime,
    ) -> IntradayBarsResult:
        require_aware(as_of, "as_of")
        known_at = datetime.now(as_of.tzinfo)
        series: dict[str, IntradayBarSeries] = {}
        for period in periods:
            series[period] = await self._read_period(code, period, as_of=as_of)
        return IntradayBarsResult(
            code=code, as_of=as_of, known_at=known_at,
            source="legacy-provider", periods=series,
        )

    async def _read_period(
        self, code: str, period: str, *, as_of: datetime
    ) -> IntradayBarSeries:
        try:
            result = await self._provider.get_kline(
                code, period, self._bars_per_period, adjust="raw",
            )
        except Exception as exc:
            return IntradayBarSeries(
                period=period, status="UNKNOWN",
                reason=f"{type(exc).__name__}: {exc}",
                precision="UNKNOWN",
            )
        bars = [
            bar for bar in result.klines if bar.timestamp <= as_of
        ]
        if not bars:
            return IntradayBarSeries(
                period=period, status="UNKNOWN",
                reason="NO_BARS_KNOWN_AT_AS_OF", stale=result.stale,
                precision="UNKNOWN",
            )
        mapped = tuple(
            IntradayBar(
                bar_time=bar.timestamp, open=bar.open, high=bar.high,
                low=bar.low, close=bar.close, volume=bar.volume,
                amount=bar.amount,
                bar_status="PROVISIONAL" if bar.provisional else "CLOSED",
            )
            for bar in bars
        )
        return IntradayBarSeries(
            period=period, status="AVAILABLE", bars=mapped,
            bar_count=len(mapped),
            provisional=any(bar.provisional for bar in bars),
            stale=result.stale, precision="LIMITED",
            first_bar_time=bars[0].timestamp,
            last_bar_time=bars[-1].timestamp,
        )
