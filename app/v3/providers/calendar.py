from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class TradingCalendarMetadata:
    source: str
    source_version: str
    calendar_code: str
    coverage_start: date
    coverage_end: date


class TradingCalendar(Protocol):
    @property
    def metadata(self) -> TradingCalendarMetadata: ...

    def is_trading_day(self, value: date) -> bool: ...
