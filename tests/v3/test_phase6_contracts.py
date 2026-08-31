from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.v3.domain.context import (
    CANDIDATE_COMPARISON_SCHEMA_VERSION,
    CONTEXT_PACK_SCHEMA_VERSION,
    CandidateComparisonMember,
    CandidateComparisonPack,
    ContextEvidenceSelection,
    ContextLevel,
    ContextPack,
    ContextSubjectType,
    EvidenceSelectionSide,
)
from app.v3.domain.task import (
    ExpectedRun,
    TaskGroupCounts,
    TaskProfile,
    TaskRun,
    TaskRunStatus,
)
from app.v3.repositories.protocols import (
    CandidateComparisonRepository,
    ContextPackRepository,
    TaskRegistryRepository,
)


NOW = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def comparison_members(count: int = 20) -> tuple[CandidateComparisonMember, ...]:
    return tuple(
        CandidateComparisonMember(
            security_id=uuid4(),
            candidate_order=index,
            market="SH",
            code=f"60{index:04d}",
            name=f"测试股票{index}",
            recall_summary={"channels": ["breakout"]},
            trend_summary={"return_20d": 0.1},
            coverage=0.9,
            stale=False,
        )
        for index in range(1, count + 1)
    )


def build_comparison(**overrides) -> CandidateComparisonPack:
    values = {
        "candidate_set_id": uuid4(),
        "builder_version": "comparison-builder.v1",
        "schema_version": CANDIDATE_COMPARISON_SCHEMA_VERSION,
        "field_profile_version": "compact-fields.v1",
        "universe_snapshot_id": uuid4(),
        "feature_run_id": uuid4(),
        "recall_run_id": uuid4(),
        "regime_snapshot_id": uuid4(),
        "as_of": NOW,
        "known_at": NOW + timedelta(seconds=1),
        "coverage": 0.9,
        "members": comparison_members(),
    }
    values.update(overrides)
    return CandidateComparisonPack.build(**values)


def build_profile(**overrides) -> TaskProfile:
    values = {
        "profile_code": "POST_MARKET",
        "version": 1,
        "schedule": "0 16 * * 1-5",
        "timezone": "Asia/Shanghai",
        "trading_calendar_source": "UNKNOWN",
        "trading_calendar_version": "UNKNOWN",
        "context_level": ContextLevel.NORMAL,
        "comparison_first": True,
        "candidate_limit": 100,
        "topk_limit": 10,
        "topk_context_level": ContextLevel.DEEP,
        "output_schema": {"type": "CandidateComparisonResult"},
        "expected_group_count": 10,
        "grace_seconds": 3600,
        "strategy_version": "strategy.v1",
    }
    values.update(overrides)
    return TaskProfile.build(**values)


def test_comparison_pack_preserves_20_to_100_contiguous_candidates() -> None:
    pack = build_comparison()
    assert len(pack.members) == 20
    assert [member.candidate_order for member in pack.members] == list(range(1, 21))
    assert pack.content_hash == pack.computed_content_hash()

    with pytest.raises(ValidationError, match="at least 20"):
        build_comparison(members=comparison_members(19))
    reordered = list(comparison_members())
    reordered[0] = reordered[0].model_copy(update={"candidate_order": 2})
    with pytest.raises(ValidationError, match="contiguous input order"):
        build_comparison(members=tuple(reordered))

    integer_coverage = build_comparison(coverage=1)
    assert integer_coverage.coverage == 1.0
    assert integer_coverage.content_hash == integer_coverage.computed_content_hash()


def test_comparison_member_rejects_final_score_fields() -> None:
    payload = comparison_members(1)[0].model_dump()
    payload["quality"]["final_total_score"] = 99
    with pytest.raises(ValidationError, match="cannot contain a unified final score"):
        CandidateComparisonMember(**payload)


def test_context_pack_enforces_budget_and_point_in_time_evidence() -> None:
    selection = ContextEvidenceSelection(
        evidence_id=uuid4(),
        evidence_known_at=NOW - timedelta(seconds=1),
        selection_reason="官方公告与候选风险直接相关",
        side=EvidenceSelectionSide.CONTRARY,
        retrieval_score=0.9,
        relevance=0.8,
        source_priority=0,
        final_order=1,
    )
    values = {
        "context_level": ContextLevel.NORMAL,
        "subject_type": ContextSubjectType.SECURITY,
        "subject_id": "SH.600001",
        "task_profile_id": uuid4(),
        "task_profile_version": 1,
        "builder_version": "context-builder.v1",
        "schema_version": CONTEXT_PACK_SCHEMA_VERSION,
        "as_of": NOW,
        "known_at": NOW + timedelta(seconds=1),
        "universe_snapshot_id": uuid4(),
        "feature_run_id": uuid4(),
        "token_budget": 6_000,
        "actual_tokens": 5_200,
        "coverage": 0.8,
        "payload": {"untrusted_evidence": []},
        "evidence_selections": (selection,),
    }
    pack = ContextPack.build(**values)
    assert pack.content_hash == pack.computed_content_hash()

    with pytest.raises(ValidationError, match="outside FAST range"):
        ContextPack.build(**{**values, "context_level": ContextLevel.FAST})
    future = selection.model_copy(update={"evidence_known_at": NOW + timedelta(seconds=1)})
    with pytest.raises(ValidationError, match="later than context as_of"):
        ContextPack.build(**{**values, "evidence_selections": (future,)})


def test_task_profile_freezes_comparison_settings_and_hash() -> None:
    profile = build_profile()
    assert profile.content_hash == profile.computed_content_hash()
    with pytest.raises(ValidationError, match="requires candidate/topk settings"):
        build_profile(topk_limit=None)
    with pytest.raises(ValidationError, match="cannot define candidate/topk"):
        build_profile(
            comparison_first=False,
            candidate_limit=100,
            topk_limit=None,
            topk_context_level=None,
        )


def test_expected_run_is_schedule_fact_not_ai_execution() -> None:
    fields = ExpectedRun.model_fields
    assert "ai_executed" not in fields
    assert "result" not in fields
    run = ExpectedRun.build(
        task_profile_id=uuid4(),
        task_profile_version=1,
        scheduled_for=NOW,
        window_end=NOW + timedelta(hours=1),
        known_at=NOW - timedelta(hours=1),
    )
    assert run.status.value == "EXPECTED"
    replay = run.model_copy(
        update={"expected_run_id": uuid4(), "known_at": NOW, "row_version": 2}
    )
    assert replay.computed_content_hash() == run.content_hash


def test_task_run_status_must_match_group_counts() -> None:
    TaskRun(
        task_profile_id=uuid4(),
        task_profile_version=1,
        status=TaskRunStatus.PARTIAL_COMPLETED,
        counts=TaskGroupCounts(expected=3, successful=1, failed=1, pending=1),
        started_at=NOW,
    )
    with pytest.raises(ValidationError, match="does not match group counts"):
        TaskRun(
            task_profile_id=uuid4(),
            task_profile_version=1,
            status=TaskRunStatus.COMPLETED,
            counts=TaskGroupCounts(expected=3, successful=1, failed=1, pending=1),
            started_at=NOW,
        )


def test_phase6_repository_protocol_is_frozen() -> None:
    assert set(CandidateComparisonRepository.__dict__) >= {
        "publish", "get", "get_by_content_hash"
    }
    assert set(ContextPackRepository.__dict__) >= {
        "publish", "get", "get_by_content_hash"
    }
    assert set(TaskRegistryRepository.__dict__) >= {
        "publish_profile",
        "get_profile",
        "get_profile_version",
        "publish_expected_run",
        "get_expected_run",
        "save_expected_run",
        "create_task_run",
        "get_task_run",
        "save_task_run",
    }
    signature = inspect.signature(TaskRegistryRepository.save_task_run)
    assert signature.parameters["expected_version"].kind is inspect.Parameter.KEYWORD_ONLY


def test_0006_implementation_matches_frozen_incremental_design() -> None:
    design = (ROOT / "docs/Phase6上下文任务实施记录.md").read_text(encoding="utf-8")
    assert "0005_multi_recall_foundation" in design
    assert "candidate_comparison_packs" in design
    assert "context_evidence_selections" in design
    assert "trading_calendar_source" in design
    assert "prevent_mutation" in design
    migration = (
        ROOT / "migrations/versions/0006_context_task_foundation.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "0006_context_task_foundation"' in migration
    assert '"0005_multi_recall_foundation"' in migration
    assert "candidate_comparison_packs" in migration
    assert "context_evidence_selections" in migration
