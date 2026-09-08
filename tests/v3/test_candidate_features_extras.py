"""候选引擎扩展指标（calculate_features._candidate_engine_extras）测试。

验证：additive extras 键真实产出、missing=None 语义、28 字段
coverage 语义不受影响、benchmark_series（(交易日, 收盘价) 对）驱动 RS 指标。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.v3.application.calculate_features import CalculateSecurityFeatureService
from app.v3.domain.market_data import (
    AdjustType, BarPeriod, BarSeriesRevision, BarSeriesRevisionContent, MarketBar,
    PointInTimePrecision,
)

NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


def _revision_from(closes: list[float]) -> BarSeriesRevision:
    bars = tuple(
        MarketBar(
            bar_time=NOW - timedelta(days=len(closes) - index),
            open=close * 0.99,
            high=close * 1.02,
            low=close * 0.98,
            close=close,
            volume=1_000_000 + index * 1_000,
            amount=2_000_000 + index * 2_000,
            fetch_time=NOW,
        )
        for index, close in enumerate(closes)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=uuid4(), period=BarPeriod.DAY,
        adjust_type=AdjustType.QFQ, source="fixture", upstream_source="fixture",
        raw_bar_available=True, factor_revision_id=uuid4(),
        point_in_time_precision=PointInTimePrecision.FULL, known_at=NOW, bars=bars,
    ))


def _weekly_from(closes: list[float]) -> BarSeriesRevision:
    bars = tuple(
        MarketBar(
            bar_time=NOW - timedelta(weeks=len(closes) - index),
            open=close * 0.99, high=close * 1.01, low=close * 0.98,
            close=close, volume=10_000, amount=200_000, fetch_time=NOW,
        )
        for index, close in enumerate(closes)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=uuid4(), period=BarPeriod.WEEK,
        adjust_type=AdjustType.QFQ, source="fixture", upstream_source="fixture",
        raw_bar_available=True, factor_revision_id=uuid4(),
        point_in_time_precision=PointInTimePrecision.FULL, known_at=NOW, bars=bars,
    ))


def _rising_closes(n: int = 260) -> list[float]:
    return [10.0 * (1 + 0.002) ** i for i in range(n)]


def test_extras_keys_present_with_rising_market() -> None:
    service = CalculateSecurityFeatureService()
    closes = _rising_closes()
    # P0-04：基准以 (交易日, 收盘价) 序列传入，日期轴与个股日K一致
    bench_dates = [NOW - timedelta(days=len(closes) - i) for i in range(len(closes))]
    bench = list(zip(
        bench_dates,
        (10.0 * (1 + 0.0005) ** i for i in range(len(closes))),
    ))
    result = service.execute(
        feature_run_id=uuid4(), revision=_revision_from(closes), as_of=NOW,
        benchmark_series=bench,
    )
    extras = result.features
    # RV / BT / AC / NonChase / RS 关键键全部存在
    for key in (
        "macd_hist", "macd_hist_delta", "ma20_slope_delta", "ma20_ma60_gap",
        "price_ma20_distance", "swing_low_trend", "atr_contraction",
        "daily_range_contraction", "down_volume_ratio", "up_down_volume_ratio",
        "low_slope_20", "base_duration", "obv_slope_z", "volume_5_20",
        "consecutive_up_days", "volume_spike", "distance_250d_high",
        "breakout_extension_20d",
        "weekly_slope_8w", "weekly_decline_deceleration",
        "rs_5d_slope", "rs_20d_slope", "rs_slope_delta", "rs_low_higher",
    ):
        assert key in extras, f"missing extras key: {key}"
    # 上涨市：相对基准走强 → RS 斜率为正
    assert extras["rs_5d_slope"] is not None and extras["rs_5d_slope"] > 0
    assert extras["macd_hist"] is not None and extras["macd_hist"] > 0
    # 28 字段 coverage 语义不变
    assert result.coverage == 26 / 28


def test_extras_missing_semantics_none_not_zero() -> None:
    service = CalculateSecurityFeatureService()
    # 15 根日K：所有窗口指标（≥21 根）缺数据 → None 而非 0
    result = service.execute(
        feature_run_id=uuid4(), revision=_revision_from(_rising_closes(15)), as_of=NOW,
    )
    extras = result.features
    assert extras["macd_hist"] is None
    assert extras["atr_contraction"] is None
    assert extras["obv_slope_z"] is None
    assert extras["rs_5d_slope"] is None  # 无 benchmark
    assert extras["weekly_decline_deceleration"] is None  # 无周K revision


def test_weekly_deceleration_present_when_week_revision_given() -> None:
    service = CalculateSecurityFeatureService()
    closes = _rising_closes()
    weekly = [10.0] * 17
    result = service.execute(
        feature_run_id=uuid4(), revision=_revision_from(closes), as_of=NOW,
        weekly_revision=_weekly_from(weekly),
    )
    assert result.features["weekly_slope_8w"] is not None
    assert result.features["weekly_decline_deceleration"] is not None


def test_short_week_series_yields_none_weekly_metrics() -> None:
    service = CalculateSecurityFeatureService()
    result = service.execute(
        feature_run_id=uuid4(), revision=_revision_from(_rising_closes()), as_of=NOW,
        weekly_revision=_weekly_from([10.0] * 10),
    )
    assert result.features["weekly_slope_8w"] is None


def test_benchmark_missing_leaves_rs_none_but_others_computed() -> None:
    service = CalculateSecurityFeatureService()
    result = service.execute(
        feature_run_id=uuid4(), revision=_revision_from(_rising_closes()), as_of=NOW,
    )
    extras = result.features
    assert extras["rs_5d_slope"] is None
    assert extras["macd_hist"] is not None
    assert extras["consecutive_up_days"] is not None
    assert math.isfinite(float(extras["volume_5_20"]))
