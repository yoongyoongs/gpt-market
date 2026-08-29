from __future__ import annotations

import calendar
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

from app.utils.time import SHANGHAI
from app.v3.domain.market_data import (
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    MarketBar,
)
from app.v3.providers.calendar import TradingCalendar


@dataclass(frozen=True)
class LocalAggregationResult:
    period: BarPeriod
    revision: BarSeriesRevision | None
    partial_bars: tuple[MarketBar, ...]


class AggregateDailyBarsService:
    def __init__(
        self,
        trading_calendar: TradingCalendar,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._calendar = trading_calendar
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(
        self, source: BarSeriesRevision, target_period: BarPeriod
    ) -> LocalAggregationResult:
        if source.period is not BarPeriod.DAY:
            raise ValueError("local week/month aggregation requires a DAY revision")
        if target_period not in {BarPeriod.WEEK, BarPeriod.MONTH}:
            raise ValueError("target_period must be WEEK or MONTH")
        groups: OrderedDict[tuple[int, ...], list[MarketBar]] = OrderedDict()
        for bar in source.bars:
            local = bar.bar_time.astimezone(SHANGHAI)
            key = (
                tuple(local.isocalendar()[:2])
                if target_period is BarPeriod.WEEK
                else (local.year, local.month)
            )
            groups.setdefault(key, []).append(bar)

        completed: list[MarketBar] = []
        partial: list[MarketBar] = []
        now = self._clock().astimezone(SHANGHAI)
        for rows in groups.values():
            aggregated = self._aggregate(rows)
            period_end = self._period_end(aggregated.bar_time, target_period)
            last_session = self._last_trading_day(period_end, target_period)
            is_complete = self._is_complete(aggregated, last_session, now)
            if is_complete:
                completed.append(aggregated)
            else:
                partial.append(aggregated.model_copy(update={"provisional": True}))

        revision = None
        if completed:
            revision = BarSeriesRevision.build(
                BarSeriesRevisionContent(
                    revision_id=uuid4(),
                    security_id=source.security_id,
                    period=target_period,
                    adjust_type=source.adjust_type,
                    source="local_aggregate:day",
                    upstream_source=source.content_hash,
                    raw_bar_available=source.raw_bar_available,
                    factor_revision_id=source.factor_revision_id,
                    point_in_time_precision=source.point_in_time_precision,
                    precision_reason=source.precision_reason,
                    known_at=max(self._clock(), source.known_at),
                    bars=tuple(completed),
                )
            )
        return LocalAggregationResult(target_period, revision, tuple(partial))

    @staticmethod
    def _aggregate(rows: list[MarketBar]) -> MarketBar:
        ordered = sorted(rows, key=lambda item: item.bar_time)
        return MarketBar(
            bar_time=ordered[-1].bar_time,
            open=ordered[0].open,
            high=max(item.high for item in ordered),
            low=min(item.low for item in ordered),
            close=ordered[-1].close,
            volume=sum(item.volume for item in ordered),
            amount=(
                sum(item.amount for item in ordered if item.amount is not None)
                if all(item.amount is not None for item in ordered)
                else None
            ),
            provisional=False,
            fetch_time=max(item.fetch_time for item in ordered),
        )

    @staticmethod
    def _period_end(bar_time: datetime, period: BarPeriod) -> date:
        value = bar_time.astimezone(SHANGHAI).date()
        if period is BarPeriod.WEEK:
            return value + timedelta(days=6 - value.weekday())
        return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])

    def _last_trading_day(self, period_end: date, period: BarPeriod) -> date:
        value = period_end
        lower_bound = period_end - timedelta(days=6 if period is BarPeriod.WEEK else 30)
        while value >= lower_bound:
            if self._calendar.is_trading_day(value):
                return value
            value -= timedelta(days=1)
        raise ValueError(f"calendar has no trading day for {period.value} ending {period_end}")

    @staticmethod
    def _is_complete(bar: MarketBar, last_session: date, now: datetime) -> bool:
        last_bar_date = bar.bar_time.astimezone(SHANGHAI).date()
        if last_bar_date < last_session:
            return False
        return now.date() > last_session or (
            now.date() == last_session and now.time() >= time(15, 10)
        )
