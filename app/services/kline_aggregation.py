from __future__ import annotations

import calendar
from collections import OrderedDict
from datetime import datetime, time as datetime_time, timedelta

from app.models import Kline
from app.utils.time import SHANGHAI


MINUTE_PERIODS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}


def is_minute_period(period: str) -> bool:
    return period in MINUTE_PERIODS


def cache_trade_key(period: str, timestamp: datetime) -> str:
    if is_minute_period(period):
        return timestamp.isoformat()
    return timestamp.date().isoformat()


def aggregate_day_klines(klines: list[Kline], target_period: str, now: datetime, limit: int) -> list[Kline]:
    if target_period not in {"week", "month"}:
        raise ValueError("target_period must be week or month")
    groups: OrderedDict[tuple[int, int] | tuple[int, int, int], list[Kline]] = OrderedDict()
    for item in sorted(klines, key=lambda value: value.timestamp):
        local = item.timestamp.astimezone(SHANGHAI)
        key = local.isocalendar()[:2] if target_period == "week" else (local.year, local.month, 0)
        groups.setdefault(key, []).append(item)
    aggregated = [
        _aggregate_group(rows, _is_partial_day_group(target_period, rows[-1].timestamp, now))
        for rows in groups.values()
        if rows
    ]
    return aggregated[-limit:]


def aggregate_5m_klines(klines: list[Kline], target_period: str, now: datetime, limit: int) -> list[Kline]:
    if target_period not in {"15m", "30m", "60m"}:
        raise ValueError("target_period must be 15m, 30m or 60m")
    target_minutes = MINUTE_PERIODS[target_period]
    groups: OrderedDict[tuple[str, str, datetime], list[Kline]] = OrderedDict()
    for item in sorted(klines, key=lambda value: value.timestamp):
        bucket = _minute_bucket_end(item.timestamp, target_minutes)
        if bucket is None:
            continue
        session_name, bucket_end = bucket
        groups.setdefault((item.timestamp.date().isoformat(), session_name, bucket_end), []).append(item)
    aggregated = [
        _aggregate_group(rows, _is_partial_minute_group(rows, bucket_end, target_minutes, now))
        for (_, _, bucket_end), rows in groups.items()
        if rows
    ]
    return aggregated[-limit:]


def mark_latest_bar_partial(klines: list[Kline], period: str, now: datetime) -> list[Kline]:
    if not klines:
        return klines
    latest = klines[-1]
    partial = False
    if period in {"week", "month"}:
        partial = _is_partial_day_group(period, latest.timestamp, now)
    elif is_minute_period(period):
        bucket = _minute_bucket_end(latest.timestamp, MINUTE_PERIODS[period])
        partial = bucket is not None and _is_partial_minute_group(
            [latest], bucket[1], MINUTE_PERIODS[period], now, check_count=False
        )
    elif period == "day":
        local_now = now.astimezone(SHANGHAI)
        partial = (
            latest.timestamp.astimezone(SHANGHAI).date() == local_now.date()
            and local_now.weekday() < 5
            and local_now.time() < datetime_time(15, 10)
        )
    if not partial or latest.provisional:
        return klines
    return [*klines[:-1], latest.model_copy(update={"provisional": True})]


def _aggregate_group(rows: list[Kline], partial: bool) -> Kline:
    ordered = sorted(rows, key=lambda value: value.timestamp)
    return Kline(
        timestamp=ordered[-1].timestamp,
        open=ordered[0].open,
        high=max(item.high for item in ordered),
        low=min(item.low for item in ordered),
        close=ordered[-1].close,
        volume=sum(item.volume for item in ordered),
        amount=sum(item.amount for item in ordered),
        provisional=partial or any(item.provisional for item in ordered),
    )


def _is_partial_day_group(target_period: str, last_timestamp: datetime, now: datetime) -> bool:
    last = last_timestamp.astimezone(SHANGHAI)
    current = now.astimezone(SHANGHAI)
    if target_period == "week":
        if last.isocalendar()[:2] != current.isocalendar()[:2]:
            return False
        if current.weekday() >= 5:
            return False
        return current.weekday() < 4 or current.time() < datetime_time(15, 10)
    if (last.year, last.month) != (current.year, current.month):
        return False
    last_day = calendar.monthrange(current.year, current.month)[1]
    return current.day < last_day or current.time() < datetime_time(15, 10)


def _minute_bucket_end(timestamp: datetime, target_minutes: int) -> tuple[str, datetime] | None:
    local = timestamp.astimezone(SHANGHAI)
    morning = _bucket_end_for_session(local, "am", datetime_time(9, 30), datetime_time(11, 30), target_minutes)
    if morning is not None:
        return morning
    return _bucket_end_for_session(local, "pm", datetime_time(13, 0), datetime_time(15, 0), target_minutes)


def _bucket_end_for_session(
    timestamp: datetime,
    session_name: str,
    start_time: datetime_time,
    end_time: datetime_time,
    target_minutes: int,
) -> tuple[str, datetime] | None:
    current = timestamp.time()
    if current < start_time or current > end_time:
        return None
    session_start = datetime.combine(timestamp.date(), start_time, tzinfo=SHANGHAI)
    elapsed = int((timestamp - session_start).total_seconds() // 60)
    bucket_index = max(0, elapsed - 1) // target_minutes
    bucket_end = session_start + timedelta(minutes=target_minutes * (bucket_index + 1))
    session_end = datetime.combine(timestamp.date(), end_time, tzinfo=SHANGHAI)
    return session_name, min(bucket_end, session_end)


def _is_partial_minute_group(
    rows: list[Kline],
    bucket_end: datetime,
    target_minutes: int,
    now: datetime,
    *,
    check_count: bool = True,
) -> bool:
    current = now.astimezone(SHANGHAI)
    if bucket_end.date() != current.date():
        return any(item.provisional for item in rows)
    expected_5m_count = target_minutes // 5
    return current < bucket_end or (check_count and len(rows) < expected_5m_count)
