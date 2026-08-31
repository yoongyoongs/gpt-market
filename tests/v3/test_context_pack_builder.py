from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.v3.application.build_context_pack import (
    BuildContextPackCommand,
    BuildContextPackService,
)
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.context import ContextBuildSource, ContextLevel, ContextSubjectType
from app.v3.domain.evidence import (
    DecayModel,
    EvidenceMatchType,
    EvidenceRepositoryPage,
    EvidenceRepositoryView,
    EvidenceSourceType,
    NormalizedEvidence,
)
from app.v3.domain.features import FeatureRun, FeatureRunStatus, MarketRegimeSnapshot


NOW = datetime(2026, 8, 31, 6, tzinfo=timezone.utc)


def _run():
    return FeatureRun(
        feature_run_id=uuid4(), as_of=NOW - timedelta(minutes=2),
        universe_snapshot_id=uuid4(), feature_version="features.v1",
        status=FeatureRunStatus.RUNNING, expected_count=20,
        successful_count=20, failed_count=0, coverage=1.0,
        bar_revision_set_hash="1" * 64, input_manifest={},
        started_at=NOW - timedelta(minutes=3),
    ).published(completed_at=NOW - timedelta(minutes=1))


def _source():
    run = _run()
    regime = MarketRegimeSnapshot.build(
        regime_snapshot_id=uuid4(), feature_run_id=run.feature_run_id,
        as_of=run.as_of, known_at=NOW - timedelta(minutes=1),
        index_states={"SH": "UP"}, breadth={"up": 3000},
        turnover={"amount": 1_000_000.0}, risk_appetite_facts={"state": "NORMAL"},
        coverage=1.0, confidence=0.9, stale=False,
    )
    return ContextBuildSource(feature_run=run, regime=regime, recall_run_id=uuid4())


def _evidence(index: int, *, contrary=False):
    known_at = NOW - timedelta(seconds=index + 1)
    record = NormalizedEvidence.build(
        raw_document_id=uuid4(), evidence_type=EvidenceType.NEWS,
        source_type=EvidenceSourceType.NEWS, source_priority=50,
        subject_type="MARKET", subject_id="A_SHARE",
        claim_key=f"news:{index}", source="fixture", upstream_source="fixture.test",
        payload={"raw": "never-copy"},
        normalized_payload={
            "side": "CONTRARY" if contrary else "SUPPORT",
            "summary": "x" * 800,
        },
        fetch_time=known_at, known_at=known_at, confidence=0.8,
        relevance=0.9 - index / 100, decay_model=DecayModel.NONE,
        parser_version="fixture.v1",
    )
    return EvidenceRepositoryView(
        record=record, match_type=EvidenceMatchType.DIRECT,
        conflict_status="OPEN" if contrary else "NONE",
    )


class _ContextRepository:
    def __init__(self, source):
        self.source = source
        self.by_hash = {}

    async def load_source(self, **kwargs):
        return self.source

    async def publish(self, pack):
        if pack.content_hash in self.by_hash:
            return False
        self.by_hash[pack.content_hash] = pack
        return True

    async def get_by_content_hash(self, content_hash):
        return self.by_hash.get(content_hash)


class _EvidenceRepository:
    def __init__(self, views):
        self.views = views

    async def retrieve_view(self, *, query):
        return EvidenceRepositoryPage(views=self.views, coverage_counts={})


class _CandidateRepository:
    async def get(self, comparison_pack_id):
        return None


class _Uow:
    def __init__(self, contexts, evidence):
        self.context_packs = contexts
        self.evidence = evidence
        self.candidate_comparisons = _CandidateRepository()

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def commit(self): return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "budget", "limit"),
    ((ContextLevel.FAST, 3000, 8), (ContextLevel.NORMAL, 6500, 20), (ContextLevel.DEEP, 12000, 40)),
)
async def test_context_levels_enforce_budget_selection_and_untrusted_boundary(level, budget, limit):
    source = _source()
    contexts = _ContextRepository(source)
    views = tuple(_evidence(index, contrary=index == 14) for index in range(15))
    evidence = _EvidenceRepository(views)
    service = BuildContextPackService(
        lambda: _Uow(contexts, evidence), clock=lambda: NOW
    )
    command = BuildContextPackCommand(
        context_level=level, subject_type=ContextSubjectType.MARKET,
        subject_id="A_SHARE", task_profile_id=uuid4(), task_profile_version=1,
        as_of=NOW,
    )

    pack = await service.execute(command)
    replay = await service.execute(command)

    assert replay == pack
    assert pack.token_budget == budget
    assert pack.actual_tokens <= budget
    assert len(pack.evidence_selections) <= limit
    assert pack.payload["evidence"]["boundary"] == "UNTRUSTED_DATA"
    assert pack.evidence_selections[0].side.value == "CONTRARY"
    assert "never-copy" not in str(pack.payload)
    assert [item.final_order for item in pack.evidence_selections] == list(
        range(1, len(pack.evidence_selections) + 1)
    )
    assert pack.content_hash == pack.computed_content_hash()
