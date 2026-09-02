"""RT-08：市场盘中状态服务（实时方案 §19/§27 RT-08）。

以确定性规则回答"现在处于什么交易时段"：
OPEN / LUNCH_BREAK / PRE_OPEN / CLOSED；非交易日恒为 CLOSED。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable

SHANGHAI = timezone(timedelta(hours=8))


class MarketIntradayStatusService:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        is_trading_day: Callable[[date], bool],
        source: str = "intraday-status-v1",
    ) -> None:
        self._clock = clock
        self._is_trading_day = is_trading_day
        self._source = source

    async def execute(self) -> dict:
        return self._build()

    def execute_sync(self) -> dict:
        return self._build()

    def _build(self) -> dict:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local = now.astimezone(SHANGHAI)
        day = local.date()
        trading_day = day.weekday() < 5 and self._is_trading_day(day)
        minute = local.hour * 60 + local.minute
        if not trading_day:
            session = "CLOSED"
        elif minute < 9 * 60 + 30:
            session = "PRE_OPEN"
        elif minute < 11 * 60 + 30:
            session = "OPEN"
        elif minute < 13 * 60:
            session = "LUNCH_BREAK"
        elif minute < 15 * 60:
            session = "OPEN"
        else:
            session = "CLOSED"
        return {
            "source": self._source,
            "known_at": now,
            "local_time": local.isoformat(),
            "trade_date": day.isoformat(),
            "is_trading_day": trading_day,
            "session": session,
        }
