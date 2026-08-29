from __future__ import annotations

from datetime import date
from typing import Protocol


class TradingCalendar(Protocol):
    def is_trading_day(self, value: date) -> bool: ...
