from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.v3.application.evaluate_recall_channels import feature_recall_channels
from app.v3.domain.features import SecurityFeature
from app.v3.domain.hashing import canonical_hash
from app.v3.providers.recall import RecallChannelUnavailable


NOW = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)


def feature(**updates) -> SecurityFeature:
    values = {
        "feature_run_id": uuid4(), "security_id": uuid4(),
        "series_revision_id": uuid4(), "as_of": NOW, "close": 10.0,
        "return_3d": 0.04, "return_5d": 0.05, "return_20d": 0.1,
        "position_60d": 0.3, "ma20_slope": 0.005,
        "breakout_20d": True, "pullback_20d": True,
        "volume_ratio_5d": 1.8, "volume_expansion": True,
        "relative_index_strength": 0.04, "relative_industry_strength": 0.05,
        "coverage": 1.0, "stale": False,
        "features": {"weekly_state": "BASE", "daily_trend_state": "UP"},
        "input_hash": canonical_hash({"fixture": str(uuid4())}),
    }
    values.update(updates)
    return SecurityFeature.build(**values)


def test_all_feature_channels_produce_explainable_rankable_hits() -> None:
    channels = feature_recall_channels()
    assert len(channels) == 9
    for channel in channels:
        evaluation = channel.evaluate((feature(),))
        assert evaluation.evaluated_count == 1
        assert len(evaluation.candidates) == 1
        candidate = evaluation.candidates[0]
        assert 0 <= candidate.strength <= 1
        assert candidate.reasons
        assert set(candidate.matched_features) == set(
            channel.channel.configuration["required_fields"]
        )


def test_missing_required_field_marks_channel_unavailable_instead_of_faking_no_hit() -> None:
    relative_industry = next(
        channel for channel in feature_recall_channels()
        if channel.channel.code == "RELATIVE_INDUSTRY_STRENGTH"
    )
    with pytest.raises(RecallChannelUnavailable, match="no non-stale rows"):
        relative_industry.evaluate((feature(relative_industry_strength=None),))


def test_channel_sort_is_deterministic_and_stale_rows_never_hit() -> None:
    channel = next(
        item for item in feature_recall_channels() if item.channel.code == "TREND_IGNITION"
    )
    weak = feature(return_5d=0.03)
    strong = feature(return_5d=0.09)
    stale = feature(return_5d=0.2, stale=True)
    result = channel.evaluate((weak, stale, strong))
    assert [item.security_id for item in result.candidates] == [
        strong.security_id, weak.security_id,
    ]
    assert result.unavailable_count == 1
