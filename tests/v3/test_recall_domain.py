from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.v3.domain.hashing import canonical_hash
from app.v3.domain.recall import (
    ObservationStatus,
    PerformanceObservation,
    RawOpportunity,
    RecallChannel,
    RecallMissEvaluation,
    RecallResult,
    RecallRun,
    RecallRunStatus,
)


NOW = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)


def test_recall_channel_run_result_and_raw_are_content_addressed_without_final_score() -> None:
    channel = RecallChannel.build(
        code="FIRST_BREAKOUT",
        version="v1",
        configuration={"breakout_20d": True},
        description="首次突破近二十日区间",
    )
    run = RecallRun.build(
        feature_run_id=uuid4(),
        strategy_version="multi-recall-v1",
        channel_set_hash=canonical_hash([channel.content_hash]),
        as_of=NOW,
        known_at=NOW + timedelta(minutes=1),
        status=RecallRunStatus.PUBLISHED,
        expected_channel_count=1,
        successful_channel_count=1,
        failed_channel_count=0,
        security_count=10,
        hit_security_count=1,
        coverage=1,
    )
    result = RecallResult.build(
        recall_run_id=run.recall_run_id,
        channel_id=channel.channel_id,
        security_id=uuid4(),
        channel_rank=1,
        strength=0.8,
        reasons=("breakout_20d=true",),
        matched_features={"breakout_20d": True},
        coverage=1,
    )
    raw = RawOpportunity.build(
        recall_run_id=run.recall_run_id,
        security_id=result.security_id,
        as_of=NOW,
        known_at=NOW + timedelta(minutes=1),
        recall_result_ids=(result.recall_result_id,),
        channel_codes=(channel.code,),
        reason_summary={channel.code: result.reasons},
    )
    assert len({channel.content_hash, run.content_hash, result.content_hash, raw.content_hash}) == 4
    assert "score" not in raw.model_dump_json().lower()


def test_recall_run_requires_complete_channel_counts() -> None:
    with pytest.raises(ValidationError, match="channel counts must be complete"):
        RecallRun.build(
            feature_run_id=uuid4(), strategy_version="v1",
            channel_set_hash=canonical_hash(["one"]), as_of=NOW, known_at=NOW,
            status=RecallRunStatus.PUBLISHED, expected_channel_count=2,
            successful_channel_count=1, failed_channel_count=0,
            security_count=1, hit_security_count=0, coverage=0.5,
        )


def test_recall_run_identity_is_stable_across_replay_time() -> None:
    values = {
        "feature_run_id": uuid4(), "strategy_version": "v1",
        "channel_set_hash": canonical_hash(["one"]), "as_of": NOW,
        "status": RecallRunStatus.PUBLISHED, "expected_channel_count": 1,
        "successful_channel_count": 1, "failed_channel_count": 0,
        "security_count": 1, "hit_security_count": 1, "coverage": 1,
    }
    first = RecallRun.build(**values, known_at=NOW)
    replay = RecallRun.build(**values, known_at=NOW + timedelta(minutes=5))
    assert first.content_hash == replay.content_hash


def test_pending_observation_cannot_leak_future_results() -> None:
    values = {
        "recall_run_id": uuid4(), "security_id": uuid4(),
        "horizon_sessions": 5, "status": ObservationStatus.PENDING,
        "as_of": NOW, "matures_at": NOW + timedelta(days=7),
        "known_at": NOW, "baseline_price": 10,
    }
    pending = PerformanceObservation.build(**values)
    assert pending.future_price is None
    with pytest.raises(ValidationError, match="pending observation cannot contain future"):
        PerformanceObservation.build(**values, future_price=12, raw_return=0.2)
    with pytest.raises(ValidationError, match="requires mature time"):
        PerformanceObservation.build(
            **{**values, "status": ObservationStatus.MATURED},
            future_price=12, raw_return=0.2,
        )


def test_recall_miss_type_is_only_valid_for_exceptional_unrecalled_result() -> None:
    valid = RecallMissEvaluation.build(
        observation_id=uuid4(), threshold_version="top-decile-v1",
        threshold_spec={"metric": "excess_return", "quantile": 0.9},
        was_recalled=False, is_exceptional=True, miss_type="NO_CHANNEL_MATCH",
        evaluated_at=NOW, known_at=NOW,
    )
    assert valid.miss_type == "NO_CHANNEL_MATCH"
    with pytest.raises(ValidationError, match="miss_type is required only"):
        RecallMissEvaluation.build(
            observation_id=uuid4(), threshold_version="top-decile-v1",
            threshold_spec={"quantile": 0.9}, was_recalled=True,
            is_exceptional=True, miss_type="NO_CHANNEL_MATCH",
            evaluated_at=NOW, known_at=NOW,
        )
