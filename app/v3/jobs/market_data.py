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


def catchup_trade_dates(
    is_trading_day,
    *,
    last_completed: date | None,
    today: date,
    max_lookback: int = 30,
) -> tuple[date, ...]:
    """RT-05 catch-up：返回 (last_completed, today] 内需要补跑主链的交易日。

    last_completed 为 None（首次部署/无历史）时从 today - max_lookback 起，
    绝不无限回溯；周末与闭市日自动跳过。
    """
    if last_completed is None:
        start = today - timedelta(days=max_lookback)
    else:
        start = last_completed + timedelta(days=1)
    pending: list[date] = []
    candidate = start
    while candidate <= today:
        if is_trading_day(candidate):
            pending.append(candidate)
        candidate += timedelta(days=1)
    if len(pending) > max_lookback:
        pending = pending[-max_lookback:]
    return tuple(pending)
