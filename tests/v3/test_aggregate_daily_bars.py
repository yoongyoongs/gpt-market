from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from app.v3.application.aggregate_daily_bars import AggregateDailyBarsService
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    MarketBar,
    PointInTimePrecision,
)


class ExplicitCalendar:
    def __init__(self, sessions: set[date]) -> None:
        self.sessions = sessions

    def is_trading_day(self, value: date) -> bool:
        return value in self.sessions


def source_revision(days: list[date]) -> BarSeriesRevision:
    fetch_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
    bars = tuple(
        MarketBar(
            bar_time=datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc),
            open=10 + index,
            high=12 + index,
            low=9 + index,
            close=11 + index,
            volume=100,
            amount=1000,
            fetch_time=fetch_time,
        )
        for index, value in enumerate(days)
    )
    return BarSeriesRevision.build(
        BarSeriesRevisionContent(
            revision_id=uuid4(),
            security_id=uuid4(),
            period=BarPeriod.DAY,
            adjust_type=AdjustType.QFQ,
            source="eastmoney",
            upstream_source="eastmoney",
            raw_bar_available=True,
            factor_revision_id=uuid4(),
            point_in_time_precision=PointInTimePrecision.FULL,
            known_at=fetch_time,
            bars=bars,
        )
    )


def weekdays(start: date, end: date) -> set[date]:
    result = set()
    value = start
    while value <= end:
        if value.weekday() < 5:
            result.add(value)
        value += timedelta(days=1)
    return result


def test_week_uses_calendar_last_session_and_keeps_current_week_partial() -> None:
    days = [date(2026, 8, 24) + timedelta(days=index) for index in range(5)]
    days += [date(2026, 8, 31), date(2026, 9, 1)]
    calendar = ExplicitCalendar(weekdays(date(2026, 8, 24), date(2026, 9, 4)))
    service = AggregateDailyBarsService(
        calendar,
        clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
    )

    result = service.execute(source_revision(days), BarPeriod.WEEK)

    assert result.revision is not None
    assert len(result.revision.bars) == 1
    assert result.revision.bars[0].volume == 500
    assert len(result.partial_bars) == 1
    assert result.partial_bars[0].provisional is True


def test_month_closes_on_calendar_last_session_even_when_month_ends_on_weekend() -> None:
    sessions = weekdays(date(2026, 8, 1), date(2026, 8, 31))
    days = sorted(sessions)
    service = AggregateDailyBarsService(
        ExplicitCalendar(sessions),
        clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    result = service.execute(source_revision(days), BarPeriod.MONTH)

    assert result.revision is not None
    assert len(result.revision.bars) == 1
    assert result.partial_bars == ()


def test_exchange_holiday_is_not_mistaken_for_missing_bar() -> None:
    sessions = {date(2026, 10, 9)}
    service = AggregateDailyBarsService(
        ExplicitCalendar(sessions),
        clock=lambda: datetime(2026, 10, 10, tzinfo=timezone.utc),
    )
    result = service.execute(source_revision([date(2026, 10, 9)]), BarPeriod.WEEK)

    assert result.revision is not None
    assert result.partial_bars == ()
