"""Step 5 前置：候选引擎补充指标库测试（任务书 §35.1）。

覆盖 MACD hist delta / OBV slope z / swing high-low / ATR contraction /
MA slope delta / RS metrics / weekly decline deceleration / 量比系列。
missing 语义：数据不足必须 None，不得用 0 伪装。
"""

from __future__ import annotations

import math

from app.v3.candidate_engine.indicators import (
    atr_contraction,
    atr_series,
    base_duration,
    consecutive_up_days,
    daily_range_contraction,
    down_volume_ratio,
    low_slope,
    ma_slope_delta,
    macd,
    obv_slope_z,
    rs_metrics,
    swing_highs,
    swing_lows,
    swing_low_trend,
    up_down_volume_ratio,
    UP_DOWN_RATIO_CAP,
    volume_spike,
    weekly_decline_metrics,
)


def _trending_closes(n: int, start: float = 10.0, drift: float = 0.01) -> list[float]:
    return [start * (1 + drift) ** i for i in range(n)]


def _oscillating_closes(n: int, base: float = 10.0, amp: float = 0.5) -> list[float]:
    return [base + amp * math.sin(i * math.pi / 4) for i in range(n)]


def _bars_from_closes(closes: list[float]):
    highs = [c * 1.02 for c in closes]
    lows = [c * 0.98 for c in closes]
    volumes = [1_000_000.0 + 10_000 * (i % 7) for i in range(len(closes))]
    return highs, lows, volumes


class TestMACD:
    def test_insufficient_data_returns_none(self):
        hist, delta = macd([10.0] * 30)
        assert hist is None and delta is None

    def test_uptrend_positive_hist_delta(self):
        # 加速上涨：红柱扩张 → hist > 0 且 delta > 0
        closes = [10.0 * (1 + 0.005 * i / 60) ** i for i in range(60)]
        hist, delta = macd(closes)
        assert hist is not None and hist > 0
        assert delta is not None and delta > 0

    def test_steady_uptrend_hist_stable(self):
        # 恒定涨幅稳态：hist > 0，delta 在 0 附近（|delta| 远小于 hist）
        closes = _trending_closes(60)
        hist, delta = macd(closes)
        assert hist is not None and hist > 0
        assert delta is not None and abs(delta) < hist

    def test_stabilizing_decline_yields_positive_delta(self):
        # 深跌刚止住走平：绿柱收窄中 → hist_delta > 0（设计 §7.3 核心语义）
        decline = [20.0 * (0.97) ** i for i in range(40)]
        flat = [decline[-1]] * 8
        _, delta = macd(decline + flat)
        assert delta is not None and delta > 0


class TestOBV:
    def test_insufficient_returns_none(self):
        assert obv_slope_z([10.0] * 15, [100.0] * 15) is None

    def test_length_mismatch_returns_none(self):
        assert obv_slope_z([10.0] * 30, [100.0] * 10) is None

    def test_persistent_accumulation_positive(self):
        closes = _trending_closes(40)
        volumes = [1_000_000.0] * 40
        assert obv_slope_z(closes, volumes) > 0

    def test_persistent_distribution_negative(self):
        closes = [10.0 * (1 - 0.01) ** i for i in range(40)]
        volumes = [1_000_000.0] * 40
        assert obv_slope_z(closes, volumes) < 0

    def test_flat_obv_returns_zero(self):
        closes = [10.0] * 40
        volumes = [500_000.0] * 40
        assert obv_slope_z(closes, volumes) == 0.0


class TestSwing:
    def test_swing_lows_local_minima(self):
        lows = [10, 9, 8, 9, 10, 9.5, 9, 9.5, 10, 10, 10]
        indices = swing_lows(lows)
        assert 2 in indices
        assert 6 in indices

    def test_swing_highs_local_maxima(self):
        highs = [10, 11, 12, 11, 10, 11.5, 12.5, 11.5, 10, 10, 10]
        indices = swing_highs(highs)
        assert 2 in indices
        assert 6 in indices

    def test_swing_low_trend_rising(self):
        # 低点逐级抬升：8.0 -> 8.8 -> 9.4
        lows = [10, 9.5, 8.0, 9.0, 10.0, 9.6, 8.8, 9.6, 10.4, 10.0, 9.4, 10.0, 10.6, 10.6, 10.6]
        trend = swing_low_trend(lows)
        assert trend is not None and trend > 0

    def test_swing_low_trend_insufficient(self):
        assert swing_low_trend([10, 10, 10, 10]) is None

    def test_low_slope_normalized(self):
        closes = _trending_closes(30)
        highs, lows, _ = _bars_from_closes(closes)
        slope = low_slope(lows, window=20)
        assert slope is not None and slope > 0
        # 归一化后量级 < 0.1（跨价格可比）
        assert slope < 0.1


class TestATR:
    def test_atr_series_length(self):
        closes = _oscillating_closes(60)
        highs, lows, _ = _bars_from_closes(closes)
        series = atr_series(highs, lows, closes, window=20)
        assert series is not None and len(series) == 40

    def test_atr_series_insufficient(self):
        closes = _oscillating_closes(15)
        highs, lows, _ = _bars_from_closes(closes)
        assert atr_series(highs, lows, closes, window=20) is None

    def test_contraction_calming_market(self):
        # 前期大幅震荡、后期极度收窄 → contraction 接近 1
        base = [10 + 1.5 * math.sin(i * math.pi / 3) for i in range(60)]
        calm = [10.0 + 0.05 * math.sin(i * math.pi / 3) for i in range(20)]
        closes = base + calm
        highs = [c * 1.04 for c in base] + [c * 1.001 for c in calm]
        lows = [c * 0.96 for c in base] + [c * 0.999 for c in calm]
        contraction = atr_contraction(highs, lows, closes)
        assert contraction is not None and contraction > 0.5

    def test_contraction_insufficient(self):
        closes = [10.0] * 20
        highs = [10.2] * 20
        lows = [9.8] * 20
        assert atr_contraction(highs, lows, closes) is None

    def test_daily_range_contraction(self):
        base = [10 + 1.0 * math.sin(i * math.pi / 3) for i in range(20)]
        calm = [10.0 + 0.02 * math.sin(i * math.pi / 3) for i in range(10)]
        closes = base + calm
        highs = [c * 1.05 for c in base] + [c * 1.001 for c in calm]
        lows = [c * 0.95 for c in base] + [c * 0.999 for c in calm]
        value = daily_range_contraction(highs, lows, closes)
        assert value is not None and value > 0


class TestVolumeRatios:
    def test_down_volume_ratio_shrinking(self):
        # 下跌日量 100K，上涨日量 1M → 缩量比远小于 0.5
        closes = [10.0, 10.2, 9.8, 10.0] * 6
        volumes = [1_000_000.0, 1_000_000.0, 100_000.0, 100_000.0] * 6
        ratio = down_volume_ratio(closes, volumes, window=20)
        assert ratio is not None and ratio < 0.5

    def test_up_down_ratio_all_up_capped(self):
        closes = list(range(1, 30))
        volumes = [1000.0] * 29
        assert up_down_volume_ratio(closes, volumes) == UP_DOWN_RATIO_CAP

    def test_up_down_ratio_balanced(self):
        closes = [10.0, 10.2, 10.0, 10.2] * 6 + [10.0]
        volumes = [1000.0] * 25
        ratio = up_down_volume_ratio(closes, volumes, window=20)
        assert ratio is not None and 0.5 < ratio < 1.5

    def test_volume_spike(self):
        volumes = [1_000_000.0] * 24 + [5_000_000.0] * 5
        assert volume_spike(volumes) is not None and volume_spike(volumes) > 3

    def test_consecutive_up_days(self):
        closes = [10, 9, 10.1, 10.2, 10.3, 10.4]
        assert consecutive_up_days(closes) == 4

    def test_consecutive_up_days_flat_is_zero(self):
        assert consecutive_up_days([10.0, 10.0, 10.0]) == 0


class TestMASlopeDelta:
    def test_turning_from_down_to_up(self):
        # 下跌后走平回升 → delta > 0
        down = [20.0 * (1 - 0.01) ** i for i in range(30)]
        up = [down[-1] * (1 + 0.008) ** i for i in range(10)]
        assert ma_slope_delta(down + up, 20, gap=5) > 0

    def test_insufficient_returns_none(self):
        assert ma_slope_delta([10.0] * 22, 20, gap=5) is None


class TestRS:
    def test_missing_benchmark_all_none(self):
        metrics = rs_metrics([10.0] * 40, [])
        assert all(value is None for value in metrics.values())

    def test_outperforming_stock_positive_slope(self):
        bench = _trending_closes(60, drift=0.001)
        stock = _trending_closes(60, drift=0.01)
        metrics = rs_metrics(stock, bench)
        assert metrics["rs_5d_slope"] is not None and metrics["rs_5d_slope"] > 0
        assert metrics["rs_20d_slope"] is not None and metrics["rs_20d_slope"] > 0

    def test_short_series_all_none(self):
        metrics = rs_metrics([10.0] * 10, [10.0] * 10)
        assert all(value is None for value in metrics.values())


class TestWeeklyDecline:
    def test_deceleration_when_steepening_stops(self):
        # 前 8 周急跌（每周 -3%），后 8 周缓跌（每周 -0.5%）→ deceleration > 0
        steep = [100.0 * (0.97) ** i for i in range(9)]
        gentle = [steep[-1] * (0.995) ** i for i in range(1, 9)]
        recent, prev, decel = weekly_decline_metrics(steep + gentle)
        assert recent is not None and prev is not None
        assert decel is not None and decel > 0
        assert recent < 0  # 仍在下跌，但跌速减缓也算改善（设计 §6.2.4）

    def test_insufficient_weeks(self):
        assert weekly_decline_metrics([10.0] * 10) == (None, None, None)


class TestBaseDuration:
    def test_flat_base_counts(self):
        closes = [10.0] * 40
        assert base_duration(closes) is not None and base_duration(closes) > 10

    def test_insufficient(self):
        assert base_duration([10.0] * 15) is None
