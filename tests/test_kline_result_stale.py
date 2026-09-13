"""K 线新鲜度语义（P1-01 真实扫描根因修复）。

DataQualityService 的 30s 快照阈值只适用实时 quote；K 线最后 bar
天然落后于抓取时点（收盘后 60m 最后 bar 15:00，age 数小时），
聚合回退路径因此永远 stale/UNTRUSTED → Deep Rank 60m 全被拒收
（minute60_usable=0）。修复：_kline_result_stale 时段+周期感知。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from app.services.market_data_service import _kline_result_stale

CST = timezone(timedelta(hours=8))
TRADING = 300
CLOSED = 1800


def _dt(weekday_monday: int, hour: int, minute: int = 0) -> datetime:
    """2026-09-07 是周一；weekday_monday=0 → 周一。"""
    base = datetime(2026, 9, 7, hour, minute, tzinfo=CST) + timedelta(days=weekday_monday)
    return base


class TestKlineResultStaleTradingHours:
    def test_60m_recent_bar_fresh(self):
        """周二 10:30 盘中，60m 最后 bar 09:30（age 1h ≤ 2×3600）→ fresh。"""
        now = _dt(1, 10, 30)
        last_bar = _dt(1, 9, 30)
        assert _kline_result_stale(
            last_bar, "60m", now,
            trading_threshold_seconds=TRADING, closed_threshold_seconds=CLOSED,
        ) is False

    def test_60m_overnight_old_bar_stale(self):
        """盘中最后 bar 是昨日 15:00 的 60m（age 远超 2 周期）→ stale。"""
        now = _dt(1, 10, 30)
        last_bar = _dt(0, 15)
        assert _kline_result_stale(
            last_bar, "60m", now,
            trading_threshold_seconds=TRADING, closed_threshold_seconds=CLOSED,
        ) is True

    def test_5m_threshold_tighter(self):
        """5m 阈值 max(600, 300)=600：5 分钟前 fresh，20 分钟前 stale。"""
        now = _dt(1, 10, 30)
        assert _kline_result_stale(
            _dt(1, 10, 25), "5m", now,
            trading_threshold_seconds=TRADING, closed_threshold_seconds=CLOSED,
        ) is False
        assert _kline_result_stale(
            _dt(1, 10, 10), "5m", now,
            trading_threshold_seconds=TRADING, closed_threshold_seconds=CLOSED,
        ) is True


class TestKlineResultStaleClosedHours:
    def test_after_close_same_day_fresh(self):
        """周二 17:00 收盘后，最后 bar 当日 15:00 → fresh（该产出已产出）。"""
        now = _dt(1, 17)
        last_bar = _dt(1, 15)
        assert _kline_result_stale(
            last_bar, "60m", now,
            trading_threshold_seconds=TRADING, closed_threshold_seconds=CLOSED,
        ) is False

    def test_pre_open_previous_weekday_fresh(self):
        """周二 08:00 盘前，最后 bar 周一 15:00 → fresh。"""
        now = _dt(1, 8)
        last_bar = _dt(0, 15)
        assert _kline_result_stale(
            last_bar, "60m", now,
            trading_threshold_seconds=TRADING, closed_threshold_seconds=CLOSED,
        ) is False

    def test_weekend_friday_bar_fresh(self):
        """周六任意时刻，最后 bar 周五 15:00 → fresh。"""
        now = _dt(5, 12)
        last_bar = _dt(4, 15)
        assert _kline_result_stale(
            last_bar, "60m", now,
            trading_threshold_seconds=TRADING, closed_threshold_seconds=CLOSED,
        ) is False

    def test_cross_week_old_bar_stale(self):
        """周二盘后，最后 bar 上周五（跨过周末+周一）→ stale。"""
        now = _dt(1, 17)
        last_bar = _dt(1 - 4, 15)  # 上周四？9-7 周一，1-4=上周四 → < 最近交易日周一
        assert _kline_result_stale(
            last_bar, "60m", now,
            trading_threshold_seconds=TRADING, closed_threshold_seconds=CLOSED,
        ) is True

    def test_naive_datetime_treated_as_shanghai(self):
        """naive 时间按上海时区处理（不因 tzinfo 缺失误判）。"""
        now = _dt(1, 17)
        last_bar = datetime(2026, 9, 8, 15, 0)  # 周二 15:00 naive
        assert _kline_result_stale(
            last_bar, "60m", now,
            trading_threshold_seconds=TRADING, closed_threshold_seconds=CLOSED,
        ) is False
