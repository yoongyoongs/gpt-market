from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.v3.application.evaluate_recall_channels import feature_recall_channels
from app.v3.application.run_multi_recall import RunMultiRecallService
from app.v3.domain.features import FeatureRun, FeatureRunStatus
from app.v3.domain.hashing import canonical_hash
from app.v3.domain.recall import RecallChannel, RecallRunStatus
from app.v3.providers.calendar import TradingCalendarMetadata
from tests.v3.test_recall_channels import feature


NOW = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)


class Calendar:
    metadata = TradingCalendarMetadata(
        source="fixture", source_version="v1", calendar_code="XSHG",
        coverage_start=date(2020, 1, 1), coverage_end=date(2030, 1, 1),
    )

    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5


class FailingChannel:
    channel = RecallChannel.build(
        code="FAILURE_FIXTURE", version="v1", configuration={},
        description="验证单通道失败隔离",
    )

    def evaluate(self, _features):
        raise RuntimeError("fixture channel failed")


class Features:
    def __init__(self, run, rows):
        self.run = run
        self.rows = rows

    async def get_run(self, feature_run_id):
        return self.run if feature_run_id == self.run.feature_run_id else None

    async def features_for_run(self, feature_run_id):
        return self.rows if feature_run_id == self.run.feature_run_id else ()


class Recalls:
    def __init__(self):
        self.runs = {}
        self.published = None

    async def resolve_channels(self, channels):
        return {item.content_hash: item.channel_id for item in channels}

    async def get_run_by_content_hash(self, content_hash):
        return self.runs.get(content_hash)

    async def publish(self, run, results, raw_opportunities, observations):
        if run.content_hash in self.runs:
            return False
        self.runs[run.content_hash] = run
        self.published = (run, results, raw_opportunities, observations)
        return True


class Uow:
    def __init__(self, features, recalls):
        self.features = features
        self.recalls = recalls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        return None


def published_feature_run(feature_run_id, count):
    return FeatureRun(
        feature_run_id=feature_run_id, as_of=NOW,
        universe_snapshot_id=uuid4(), feature_version="fixture-v1",
        status=FeatureRunStatus.RUNNING, expected_count=count,
        successful_count=count, failed_count=0, coverage=1,
        bar_revision_set_hash=canonical_hash(["fixture"]),
        input_manifest={}, started_at=NOW,
    ).published(completed_at=NOW)


@pytest.mark.asyncio
async def test_multi_recall_isolates_channel_failure_builds_raw_union_and_full_observations() -> None:
    run_id = uuid4()
    hit = feature(feature_run_id=run_id)
    miss = feature(feature_run_id=run_id, return_5d=-0.02)
    run = published_feature_run(run_id, 2)
    features = Features(run, (hit, miss))
    recalls = Recalls()
    trend = next(
        item for item in feature_recall_channels() if item.channel.code == "TREND_IGNITION"
    )
    service = RunMultiRecallService(
        lambda: Uow(features, recalls), Calendar(),
        channels=(trend, FailingChannel()), clock=lambda: NOW + timedelta(minutes=1),
    )
    result = await service.execute(feature_run_id=run_id)
    replay = await service.execute(feature_run_id=run_id)
    assert result.status is RecallRunStatus.PUBLISHED
    assert result.successful_channel_count == result.failed_channel_count == 1
    assert result.errors == {"FAILURE_FIXTURE": "RuntimeError: fixture channel failed"}
    assert replay.recall_run_id == result.recall_run_id
    _, results, raw, observations = recalls.published
    assert len(results) == len(raw) == 1
    assert raw[0].security_id == hit.security_id
    assert len(observations) == 6
    assert {item.security_id for item in observations} == {hit.security_id, miss.security_id}
    assert all(item.future_price is None for item in observations)


@pytest.mark.asyncio
async def test_multi_recall_rejects_future_feature_run_before_publishing_channels() -> None:
    run_id = uuid4()
    row = feature(feature_run_id=run_id)
    run = published_feature_run(run_id, 1)
    features = Features(run, (row,))
    recalls = Recalls()
    trend = next(
        item for item in feature_recall_channels() if item.channel.code == "TREND_IGNITION"
    )
    service = RunMultiRecallService(
        lambda: Uow(features, recalls), Calendar(), channels=(trend,),
        clock=lambda: NOW - timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="as_of is in the future"):
        await service.execute(feature_run_id=run_id)
    assert recalls.published is None
