from __future__ import annotations

from datetime import date
from importlib.metadata import version

import exchange_calendars

from app.v3.providers.calendar import TradingCalendarMetadata


class TradingCalendarOutOfRange(ValueError):
    pass


class ExchangeCalendarsAShareCalendar:
    """Versioned A-share session calendar backed by exchange_calendars XSHG."""

    def __init__(self, calendar_code: str = "XSHG") -> None:
        self._calendar = exchange_calendars.get_calendar(calendar_code)
        self._metadata = TradingCalendarMetadata(
            source="exchange_calendars",
            source_version=version("exchange-calendars"),
            calendar_code=calendar_code,
            coverage_start=self._calendar.first_session.date(),
            coverage_end=self._calendar.last_session.date(),
        )

    @property
    def metadata(self) -> TradingCalendarMetadata:
        return self._metadata

    def is_trading_day(self, value: date) -> bool:
        if not self._metadata.coverage_start <= value <= self._metadata.coverage_end:
            raise TradingCalendarOutOfRange(
                f"{value} is outside {self._metadata.calendar_code} calendar coverage "
                f"[{self._metadata.coverage_start}, {self._metadata.coverage_end}]"
            )
        return bool(self._calendar.is_session(value.isoformat()))
