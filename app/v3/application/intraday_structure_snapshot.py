"""RT-02：多周期结构快照服务（实时方案 §4.4/§4.5/§27 RT-02）。

组合 RT-01 IntradayMarketDataService 的周/日/60/15/5 Bar，输出：

- 单周期确定性结构：趋势（UP/DOWN/SIDEWAYS/UNKNOWN）+ 支撑/压力
  （复用 DeepMarketDataService 同一条规则，不足 8 根显式 UNKNOWN）；
- weekly/daily 未收盘 K 线显式 PROVISIONAL；
- reversal_state：周降日涨 → POSSIBLE（下降趋势中的反弹候选）；
  CONFIRMED 只能由可解释证据给出，服务器绝不自行确认；
- conflict/conflict_rule：多周期冲突显式表达（与特征计算共用同一条
  _multi_timeframe 确定性规则），任一关键周期 UNKNOWN → UNKNOWN。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.v3.application.deep_market_data import DeepMarketDataService
from app.v3.domain.intraday import IntradayBar, PeriodStructure

_SNAPSHOT_PERIODS = ("week", "day", "60m", "15m", "5m")
_MIN_BARS = 8


class _BarsService(Protocol):
    async def get_intraday_bars(
        self, code: str, periods: tuple[str, ...], *, as_of: datetime
    ) -> Any: ...


class _QuoteService(Protocol):
    async def get_quote_snapshot(self, code: str, *, as_of: datetime) -> Any: ...


class IntradayStructureSnapshotService:
    def __init__(
        self,
        bars_service: _BarsService,
        quote_service: _QuoteService | None = None,
        *,
        source: str = "intraday-structure-v1",
    ) -> None:
        self._bars = bars_service
        self._quotes = quote_service
        self._source = source

    async def get_snapshot(self, code: str, *, as_of: datetime) -> Any:
        from app.v3.domain.intraday import IntradayStructureSnapshot

        result = await self._bars.get_intraday_bars(
            code, _SNAPSHOT_PERIODS, as_of=as_of
        )
        weekly = self._period_structure("week", result.periods.get("week"))
        daily = self._period_structure("day", result.periods.get("day"))
        intraday = {
            period: self._period_structure(period, result.periods.get(period))
            for period in ("60m", "15m", "5m")
        }
        conflict, rule = self._conflict(daily.trend, weekly.trend)
        latest_price = await self._latest_price(code, as_of=as_of)
        stale = any(
            bool(struct.stale)
            for struct in (weekly, daily, *intraday.values())
        )
        return IntradayStructureSnapshot(
            code=code, as_of=as_of, known_at=result.known_at,
            source=self._source, latest_price=latest_price,
            weekly=weekly, daily=daily,
            reversal_state=self._reversal_state(daily.trend, weekly.trend),
            conflict=conflict, conflict_rule=rule,
            periods=intraday, stale=stale,
        )

    @staticmethod
    def _period_structure(period: str, series: Any) -> PeriodStructure:
        if series is None or series.status != "AVAILABLE":
            reason = getattr(series, "reason", None) or "PERIOD_UNAVAILABLE"
            return PeriodStructure(
                trend="UNKNOWN", reason=reason,
                stale=getattr(series, "stale", None),
            )
        bars: list[IntradayBar] = list(series.bars)
        if len(bars) < _MIN_BARS:
            return PeriodStructure(
                trend="UNKNOWN", reason="INSUFFICIENT_BARS",
                bar_count=len(bars), stale=series.stale,
            )
        structure = DeepMarketDataService._structure(bars)
        return PeriodStructure(
            trend=structure["trend"],
            support=structure.get("support"),
            resistance=structure.get("resistance"),
            bar_status="PROVISIONAL" if series.provisional else "CLOSED",
            bar_count=len(bars), stale=series.stale,
        )

    @staticmethod
    def _reversal_state(daily: str, weekly: str) -> str:
        if daily == "UNKNOWN" or weekly == "UNKNOWN":
            return "UNKNOWN"
        # §4.5：周降日涨 → 反弹候选；CONFIRMED 需可解释证据，服务器不定
        return "POSSIBLE" if weekly == "DOWN" and daily == "UP" else "NONE"

    @staticmethod
    def _conflict(daily: str, weekly: str) -> tuple[str | None, str | None]:
        from app.v3.application.calculate_features import (
            CalculateSecurityFeatureService,
        )

        state, rule = CalculateSecurityFeatureService._multi_timeframe(
            daily, weekly
        )
        return state, rule

    async def _latest_price(self, code: str, *, as_of: datetime) -> float | None:
        if self._quotes is None:
            return None
        try:
            quote = await self._quotes.get_quote_snapshot(code, as_of=as_of)
        except Exception:
            return None
        return quote.last_price
