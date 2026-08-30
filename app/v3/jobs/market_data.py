from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.utils.time import SHANGHAI
from app.v3.providers.calendar import TradingCalendar


def latest_completed_session(
    calendar: TradingCalendar,
    now: datetime,
    *,
    close_grace: time = time(15, 10),
) -> date:
    local_now = now.astimezone(SHANGHAI)
    candidate = local_now.date()
    if local_now.time() < close_grace:
        candidate -= timedelta(days=1)
    for _ in range(370):
        if calendar.is_trading_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise RuntimeError("no completed trading session found within 370 days")
