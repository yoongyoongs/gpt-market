from __future__ import annotations

from datetime import date, datetime, timezone

from app.v3.jobs.market_data import latest_completed_session


class ExplicitCalendar:
    def __init__(self, sessions: set[date]) -> None:
        self.sessions = sessions

    def is_trading_day(self, value: date) -> bool:
        return value in self.sessions


def test_latest_completed_session_uses_friday_on_sunday() -> None:
    calendar = ExplicitCalendar({date(2026, 8, 28)})

    assert latest_completed_session(
        calendar, datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
    ) == date(2026, 8, 28)


def test_latest_completed_session_does_not_publish_current_open_day() -> None:
    calendar = ExplicitCalendar({date(2026, 8, 27), date(2026, 8, 28)})

    assert latest_completed_session(
        calendar, datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)
    ) == date(2026, 8, 27)
