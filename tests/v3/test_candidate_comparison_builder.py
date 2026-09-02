from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.v3.application.build_candidate_comparison import (
    BuildCandidateComparisonService,
    CandidateComparisonQuery,
)
from app.v3.domain.context import (
    CandidateComparisonRecallHit,
    CandidateComparisonSource,
    CandidateComparisonSourceMember,
)
from app.v3.domain.features import (
    FeatureRun,
    FeatureRunStatus,
    PublishedSecurityFeatureView,
    SecurityFeature,
)
from app.v3.domain.hashing import canonical_hash


NOW = datetime(2026, 8, 31, 4, tzinfo=timezone.utc)


def _feature_run() -> FeatureRun:
    return FeatureRun(
        feature_run_id=uuid4(),
        as_of=NOW - timedelta(minutes=2),
        universe_snapshot_id=uuid4(),
        feature_version="full-market-features.v1",
        status=FeatureRunStatus.RUNNING,
        expected_count=20,
        successful_count=20,
        failed_count=0,
        coverage=1,
        bar_revision_set_hash="1" * 64,
        input_manifest={},
        started_at=NOW - timedelta(minutes=3),
    ).published(completed_at=NOW - timedelta(minutes=1))


def _feature(
    run_id: UUID, security_id: UUID, index: int
) -> PublishedSecurityFeatureView:
    values = dict(
        feature_run_id=run_id,
        security_id=security_id,
        series_revision_id=uuid4(),
        as_of=NOW - timedelta(minutes=2),
        close=float(10 + index),
        return_5d=index / 100,
        return_20d=index / 50,
        position_60d=index / 20,
        ma20=10.0,
        ma60=9.5,
        ma20_slope=0.02,
        atr14=0.5,
        atr_pct=0.04,
        volatility20=0.03,
        amount=float(100_000_000 + index),
        volume_ratio_5d=1.2,
        relative_index_strength=0.01,
        coverage=0.9,
        stale=False,
        missing_fields=("relative_industry_strength",),
        source_errors=(),
        quality={"point_in_time_precision": "FULL"},
        features={
            "daily_trend_state": "UP",
            "weekly_trend_state": "DOWN",
            "multi_timeframe_state": "WEEKLY_DOWN_DAILY_BOUNCE",
            "multi_timeframe_rule": "下降趋势中的反弹",
            "liquidity_quality": "GOOD",
        },
        input_hash=canonical_hash({"index": index}),
    )
    feature = SecurityFeature.build(**values)
    return PublishedSecurityFeatureView(
        **feature.model_dump(exclude={"content_hash"}),
        source_content_hash=feature.content_hash,
    )


class _ComparisonRepository:
    def __init__(self, source: CandidateComparisonSource) -> None:
        self.source = source
        self.by_hash = {}
        self.by_set = {}

    async def load_source(self, codes, **kwargs):
        return self.source

    async def publish(self, pack):
        if pack.content_hash in self.by_hash:
            return False
        self.by_hash[pack.content_hash] = pack
        self.by_set[pack.candidate_set_id] = pack
        return True

    async def get_by_content_hash(self, content_hash):
        return self.by_hash.get(content_hash)

    async def latest_for_candidate_set(self, candidate_set_id, **kwargs):
        return self.by_set.get(candidate_set_id)


class _EvidenceRepository:
    async def for_securities(self, security_ids, *, as_of):
        return ()


class _Uow:
    def __init__(self, comparisons):
        self.candidate_comparisons = comparisons
        self.evidence = _EvidenceRepository()
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        self.committed = True


def _service():
    run = _feature_run()
    source_members = tuple(
        CandidateComparisonSourceMember(
            security_id=(security_id := uuid4()),
            market="SH" if index < 10 else "SZ",
            code=f"{600000 + index:06d}",
            name=f"候选{index + 1}",
            feature=_feature(run.feature_run_id, security_id, index),
            recall_hits=(
                CandidateComparisonRecallHit(
                    channel_code="trend",
                    channel_rank=index + 1,
                    strength=0.8,
                    reasons=("趋势命中",),
                    coverage=1,
                ),
            ),
        )
        for index in range(20)
    )
    repository = _ComparisonRepository(
        CandidateComparisonSource(
            feature_run=run,
            recall_run_id=uuid4(),
            regime_snapshot_id=uuid4(),
            members=source_members,
        )
    )
    return (
        BuildCandidateComparisonService(
            lambda: _Uow(repository), clock=lambda: NOW
        ),
        repository,
    )


@pytest.mark.asyncio
async def test_builds_compact_ordered_pack_and_replays_by_hash() -> None:
    service, repository = _service()
    codes = tuple(f"{600000 + index:06d}" for index in range(20))
    query = CandidateComparisonQuery(codes=codes, as_of=NOW)

    first = await service.execute(query)
    replay = await service.execute(query)

    assert replay == first
    assert len(repository.by_hash) == 1
    assert [member.code for member in first.members] == list(codes)
    assert [member.candidate_order for member in first.members] == list(range(1, 21))
    assert first.coverage == 0.9
    assert first.members[0].recall_summary["channels"] == ["trend"]
    # §14.2：多周期合成事实必须随候选包下发（周 DOWN + 日 UP → 反弹描述）
    trend = first.members[0].trend_summary
    assert trend["multi_timeframe_state"] == "WEEKLY_DOWN_DAILY_BOUNCE"
    assert trend["multi_timeframe_rule"] == "下降趋势中的反弹"
    serialized = first.model_dump(mode="json")
    assert "final_total_score" not in str(serialized)
    assert first.trim_summary["omitted"] == [
        "minute_bars",
        "deep_evidence_payload",
        "unified_final_score",
    ]


@pytest.mark.asyncio
async def test_reads_an_existing_pack_by_candidate_set_without_rebuilding() -> None:
    service, _ = _service()
    codes = tuple(f"{600000 + index:06d}" for index in range(20))
    built = await service.execute(CandidateComparisonQuery(codes=codes, as_of=NOW))

    loaded = await service.execute(
        CandidateComparisonQuery(candidate_set_id=built.candidate_set_id, as_of=NOW)
    )

    assert loaded == built


def test_query_rejects_invalid_candidate_sets() -> None:
    with pytest.raises(ValidationError, match="20 to 100"):
        CandidateComparisonQuery(codes=("600000",), as_of=NOW)
    with pytest.raises(ValidationError, match="must be unique"):
        CandidateComparisonQuery(codes=("600000",) * 20, as_of=NOW)
    with pytest.raises(ValidationError, match="exactly one"):
        CandidateComparisonQuery(as_of=NOW)
