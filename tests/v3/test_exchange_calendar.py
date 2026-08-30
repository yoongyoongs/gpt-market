from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version

import pytest

from app.v3.application.aggregate_daily_bars import AggregateDailyBarsService
from app.v3.domain.market_data import BarPeriod
from app.v3.infrastructure.providers.exchange_calendar import (
    ExchangeCalendarsAShareCalendar,
    TradingCalendarOutOfRange,
)
from tests.v3.test_aggregate_daily_bars import source_revision


def test_xshg_calendar_matches_official_2026_spring_festival_sessions() -> None:
    calendar = ExchangeCalendarsAShareCalendar()

    assert calendar.metadata.source == "exchange_calendars"
    assert calendar.metadata.source_version == version("exchange-calendars")
    assert calendar.metadata.calendar_code == "XSHG"
    assert calendar.is_trading_day(date(2026, 2, 23)) is False
    assert calendar.is_trading_day(date(2026, 2, 24)) is True


def test_calendar_rejects_dates_outside_versioned_coverage() -> None:
    calendar = ExchangeCalendarsAShareCalendar()

    with pytest.raises(TradingCalendarOutOfRange, match="outside XSHG calendar coverage"):
        calendar.is_trading_day(calendar.metadata.coverage_end + timedelta(days=1))


def test_post_holiday_short_week_is_published_after_last_real_session() -> None:
    calendar = ExchangeCalendarsAShareCalendar()
    service = AggregateDailyBarsService(
        calendar,
        clock=lambda: datetime(2026, 2, 27, 7, 11, tzinfo=timezone.utc),
    )
    revision = source_revision(
        [
            date(2026, 2, 24),
            date(2026, 2, 25),
            date(2026, 2, 26),
            date(2026, 2, 27),
        ]
    )

    result = service.execute(revision, BarPeriod.WEEK)

    assert result.revision is not None
    assert len(result.revision.bars) == 1
    assert result.revision.bars[0].volume == 400
    assert result.partial_bars == ()
