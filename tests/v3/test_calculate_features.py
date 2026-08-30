from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.v3.application.calculate_features import CalculateSecurityFeatureService
from app.v3.application.calculate_market_regime import CalculateMarketRegimeService
from app.v3.domain.market_data import (
    AdjustType, BarPeriod, BarSeriesRevision, BarSeriesRevisionContent, MarketBar,
    PointInTimePrecision,
)

NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


def revision(count: int = 260) -> BarSeriesRevision:
    bars = tuple(
        MarketBar(
            bar_time=NOW - timedelta(days=count - index),
            open=10 + index / 10,
            high=10.5 + index / 10,
            low=9.5 + index / 10,
            close=10.2 + index / 10,
            volume=1000 + index * 10,
            amount=1_000_000 + index * 1000,
            fetch_time=NOW,
        )
        for index in range(count)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=uuid4(), period=BarPeriod.DAY,
        adjust_type=AdjustType.QFQ, source="fixture", upstream_source="fixture",
        raw_bar_available=True, factor_revision_id=uuid4(),
        point_in_time_precision=PointInTimePrecision.FULL, known_at=NOW, bars=bars,
    ))


def test_full_feature_calculation_binds_revision_and_long_windows() -> None:
    result = CalculateSecurityFeatureService().execute(
        feature_run_id=uuid4(), revision=revision(), as_of=NOW,
    )
    assert result.return_250d is not None
    assert result.position_250d is not None
    assert result.atr14 is not None
    assert result.relative_industry_strength is None
    assert "relative_industry_strength" in result.missing_fields
    assert result.coverage == 26 / 28
    assert result.quality["adjust_type"] == "QFQ"
    assert result.stale is False


def test_short_history_is_explicitly_missing_not_substituted() -> None:
    result = CalculateSecurityFeatureService().execute(
        feature_run_id=uuid4(), revision=revision(30), as_of=NOW,
    )
    assert result.return_60d is None
    assert result.position_60d is None
    assert result.ma60 is None
    assert {"return_60d", "position_60d", "ma60"} <= set(result.missing_fields)


def test_market_regime_contains_facts_and_explicit_unknowns() -> None:
    run_id = uuid4()
    feature = CalculateSecurityFeatureService().execute(
        feature_run_id=run_id, revision=revision(), as_of=NOW,
    )
    result = CalculateMarketRegimeService().execute(
        feature_run_id=run_id, features=(feature,), as_of=NOW, known_at=NOW,
        expected_count=1,
    )
    assert result.breadth["observed"] == 1
    assert result.index_states["status"] == "UNKNOWN"
    assert "score" not in result.risk_appetite_facts
