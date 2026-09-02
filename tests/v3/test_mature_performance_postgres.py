"""RC-06A（PF-001）：Mature Engine 落库与幂等（PostgreSQL）。"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.mature_performance import MaturePerformanceService
from app.v3.domain.index_benchmark import (
    IndexBenchmarkBar,
    IndexBenchmarkRevision,
    IndexBenchmarkRevisionContent,
)
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    MarketBar,
    PointInTimePrecision,
)
from app.v3.infrastructure.db.models import (
    AIResultEnvelopeModel,
    AgentTaskModel,
    ContextPackModel,
    DecisionModel,
    EntryPlanModel,
    FeatureRunModel,
    PerformanceAttributionModel,
    SecurityModel,
    TaskProfileModel,
    TaskRunModel,
    UniverseSnapshotModel,
    UniverseSourceModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 8, 26, 8, tzinfo=timezone.utc)


def _bars_revision(security_id):
    bars = tuple(
        MarketBar(
            bar_time=datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=index),
            open=10.0 + index * 0.1, high=10.2 + index * 0.1,
            low=9.8 + index * 0.1, close=10.0 + index * 0.1,
            volume=1_000_000, amount=1e7,
            fetch_time=NOW - timedelta(minutes=1),
        )
        for index in range(25)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=security_id, period=BarPeriod.DAY,
        adjust_type=AdjustType.QFQ, source="fixture", upstream_source="fixture",
        raw_bar_available=False,
        point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="fixture QFQ only",
        known_at=NOW - timedelta(minutes=1), bars=bars,
    ))


async def test_mature_engine_persists_attributions_idempotently() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    decision_as_of = datetime(2026, 8, 12, 7, tzinfo=timezone.utc)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        security = SecurityModel(security_id=uuid4(),
                                 code=f"60{uuid4().hex[:4]}",
                                 market="SH", name="mature fixture")
        uow._session.add(security)
        await uow._session.flush()
        security_id = security.security_id
        source = UniverseSourceModel(
            source_id=uuid4(), code=f"mature-{uuid4().hex[:12]}",
            source_type="EXCHANGE", priority=1, capability_version="1",
        )
        snapshot = UniverseSnapshotModel(
            snapshot_id=uuid4(), source_id=source.source_id, as_of=NOW,
            fetch_time=NOW, known_at=NOW, coverage=Decimal("1"),
            stale=False, status="PRIMARY", content_hash=uuid4().hex,
        )
        feature_run = FeatureRunModel(
            feature_run_id=uuid4(), as_of=NOW,
            universe_snapshot_id=snapshot.snapshot_id, feature_version="v1",
            status="PUBLISHED", expected_count=1, coverage=Decimal("1"),
            bar_revision_set_hash="0" * 64, input_manifest={}, started_at=NOW,
        )
        profile = TaskProfileModel(
            task_profile_id=uuid4(), profile_code=f"profile-{uuid4().hex[:12]}",
            version=1, timezone="Asia/Shanghai", context_level="NORMAL",
            output_schema={}, content_hash=uuid4().hex,
        )
        task_run = TaskRunModel(
            task_run_id=uuid4(), task_profile_id=profile.task_profile_id,
            expected_group_count=1, pending_group_count=1,
        )
        context_pack = ContextPackModel(
            context_pack_id=uuid4(), context_level="NORMAL",
            subject_type="SECURITY", subject_id="600500",
            task_profile_id=profile.task_profile_id, task_profile_version=1,
            builder_version="v1", schema_version="v1", as_of=NOW, known_at=NOW,
            universe_snapshot_id=snapshot.snapshot_id,
            feature_run_id=feature_run.feature_run_id,
            token_budget=5000, actual_tokens=100, coverage=Decimal("1"),
            missing_fields=[], trim_summary={}, payload={}, references=[],
            content_hash=uuid4().hex,
        )
        agent_task = AgentTaskModel(
            task_id=uuid4(), task_run_id=task_run.task_run_id,
            task_type="STOCK_ANALYSIS", subject={}, task_profile="TEST_PROFILE",
            trigger_type="SCHEDULE", as_of=NOW,
            context_pack_id=context_pack.context_pack_id,
            context_pack_hash="0" * 64, expected_result_type="DecisionResult",
            content_hash=uuid4().hex,
        )
        envelope = AIResultEnvelopeModel(
            result_id=uuid4(), task_id=agent_task.task_id,
            task_run_id=task_run.task_run_id, schema_version="v1",
            result_type="DecisionResult", agent_type="AI", provider="OPENAI",
            model="test-model", context_pack_id=context_pack.context_pack_id,
            context_pack_hash="0" * 64, prompt_version="p1",
            strategy_version="test-v1", produced_at=NOW, as_of=NOW, known_at=NOW,
            evidence_ids=[], payload={}, content_hash=uuid4().hex,
        )
        plan_id = uuid4()
        plan = EntryPlanModel(
            entry_plan_id=plan_id, decision_id=uuid4(), version=1,
            source_result_id=envelope.result_id, effective_from=decision_as_of,
            expected_horizon="D3_10",
            plan={"stop_loss": "9.5", "take_profit": "10.5",
                  "entry_window_start": "2026-08-12T01:00:00+00:00",
                  "entry_window_end": "2026-08-14T07:00:00+00:00"},
            content_hash=uuid4().hex,
        )
        decision = DecisionModel(
            decision_id=plan.decision_id, security_id=security_id,
            task_run_id=task_run.task_run_id,
            context_pack_id=context_pack.context_pack_id,
            context_pack_hash="0" * 64, source_result_id=envelope.result_id,
            agent_identity={}, evidence_ids=[],
            original_entry_plan_id=plan_id,
            original_entry_plan_snapshot={"version": 1, "plan": plan.plan},
            original_entry_plan_hash="0" * 64,
            as_of=decision_as_of, produced_at=decision_as_of,
            payload={"direction": "LONG", "strategy_version": "test-v1"},
            content_hash=uuid4().hex,
        )
        for row in (source, snapshot, feature_run, profile, task_run,
                    context_pack, agent_task, envelope, decision, plan):
            uow._session.add(row)
            await uow._session.flush()
        await uow.commit()

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.bars.publish_series_revision(
            _bars_revision(security_id)
        ) is True
        await uow.commit()
    index_revision = IndexBenchmarkRevision.build(IndexBenchmarkRevisionContent(
        revision_id=uuid4(), benchmark_code="HS300", source="fixture",
        upstream_source="fixture",
        fetch_time=NOW - timedelta(minutes=2), known_at=NOW - timedelta(minutes=2),
        bars=tuple(
            IndexBenchmarkBar(
                bar_time=datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=index),
                close=3800.0 + index * 4.0, amount=1e11,
            )
            for index in range(30)
        ),
    ))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.index_benchmarks.publish(index_revision) is True
        await uow.commit()

    service = MaturePerformanceService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW
    )
    report = await service.execute(as_of=NOW)
    assert report["matured_count"] == 4  # T+1/3/5/10 成熟，T+20 待成熟
    assert report["pending_count"] == 1

    async with sessions() as session:
        rows = (await session.scalars(
            select(PerformanceAttributionModel).where(
                PerformanceAttributionModel.subject_id == plan.decision_id
            )
        )).all()
    assert len(rows) == 4
    t1 = next(row for row in rows if row.horizon_sessions == 1)
    assert float(t1.raw_return) == pytest.approx(11.2 / 11.1 - 1, abs=1e-9)
    assert t1.target_hit is True
    assert t1.metrics["direction_correctness"] is True

    second = await service.execute(as_of=NOW)
    assert second["matured_count"] == 0
    assert second["skipped_count"] == 4
    await engine.dispose()


async def test_mature_engine_rerun_with_later_clock_skips_not_crashes() -> None:
    """真实每日任务场景：同决策次日用新 clock 重跑 → 幂等 skip，绝不 PK 冲突。

    attribution_id = uuid5(decision + horizon + version) 是确定的，
    但 known_at 参与内容哈希 —— 重跑去重必须按 attribution_id 兜底。
    """
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    decision_as_of = datetime(2026, 8, 12, 7, tzinfo=timezone.utc)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        security = SecurityModel(security_id=uuid4(),
                                 code=f"60{uuid4().hex[:4]}",
                                 market="SH", name="mature rerun fixture")
        uow._session.add(security)
        await uow._session.flush()
        security_id = security.security_id
        source = UniverseSourceModel(
            source_id=uuid4(), code=f"mature-{uuid4().hex[:12]}",
            source_type="EXCHANGE", priority=1, capability_version="1",
        )
        snapshot = UniverseSnapshotModel(
            snapshot_id=uuid4(), source_id=source.source_id, as_of=NOW,
            fetch_time=NOW, known_at=NOW, coverage=Decimal("1"),
            stale=False, status="PRIMARY", content_hash=uuid4().hex,
        )
        feature_run = FeatureRunModel(
            feature_run_id=uuid4(), as_of=NOW,
            universe_snapshot_id=snapshot.snapshot_id, feature_version="v1",
            status="PUBLISHED", expected_count=1, coverage=Decimal("1"),
            bar_revision_set_hash="0" * 64, input_manifest={}, started_at=NOW,
        )
        profile = TaskProfileModel(
            task_profile_id=uuid4(), profile_code=f"profile-{uuid4().hex[:12]}",
            version=1, timezone="Asia/Shanghai", context_level="NORMAL",
            output_schema={}, content_hash=uuid4().hex,
        )
        task_run = TaskRunModel(
            task_run_id=uuid4(), task_profile_id=profile.task_profile_id,
            expected_group_count=1, pending_group_count=1,
        )
        context_pack = ContextPackModel(
            context_pack_id=uuid4(), context_level="NORMAL",
            subject_type="SECURITY", subject_id="600501",
            task_profile_id=profile.task_profile_id, task_profile_version=1,
            builder_version="v1", schema_version="v1", as_of=NOW, known_at=NOW,
            universe_snapshot_id=snapshot.snapshot_id,
            feature_run_id=feature_run.feature_run_id,
            token_budget=5000, actual_tokens=100, coverage=Decimal("1"),
            missing_fields=[], trim_summary={}, payload={}, references=[],
            content_hash=uuid4().hex,
        )
        agent_task = AgentTaskModel(
            task_id=uuid4(), task_run_id=task_run.task_run_id,
            task_type="STOCK_ANALYSIS", subject={}, task_profile="TEST_PROFILE",
            trigger_type="SCHEDULE", as_of=NOW,
            context_pack_id=context_pack.context_pack_id,
            context_pack_hash="0" * 64, expected_result_type="DecisionResult",
            content_hash=uuid4().hex,
        )
        envelope = AIResultEnvelopeModel(
            result_id=uuid4(), task_id=agent_task.task_id,
            task_run_id=task_run.task_run_id, schema_version="v1",
            result_type="DecisionResult", agent_type="AI", provider="OPENAI",
            model="test-model", context_pack_id=context_pack.context_pack_id,
            context_pack_hash="0" * 64, prompt_version="p1",
            strategy_version="test-v1", produced_at=NOW, as_of=NOW, known_at=NOW,
            evidence_ids=[], payload={}, content_hash=uuid4().hex,
        )
        plan_id = uuid4()
        plan = EntryPlanModel(
            entry_plan_id=plan_id, decision_id=uuid4(), version=1,
            source_result_id=envelope.result_id, effective_from=decision_as_of,
            expected_horizon="D3_10",
            plan={"stop_loss": "9.5", "take_profit": "10.5"},
            content_hash=uuid4().hex,
        )
        decision = DecisionModel(
            decision_id=plan.decision_id, security_id=security_id,
            task_run_id=task_run.task_run_id,
            context_pack_id=context_pack.context_pack_id,
            context_pack_hash="0" * 64, source_result_id=envelope.result_id,
            agent_identity={}, evidence_ids=[],
            original_entry_plan_id=plan_id,
            original_entry_plan_snapshot={"version": 1, "plan": plan.plan},
            original_entry_plan_hash="0" * 64,
            as_of=decision_as_of, produced_at=decision_as_of,
            payload={"direction": "LONG", "strategy_version": "test-v1"},
            content_hash=uuid4().hex,
        )
        for row in (source, snapshot, feature_run, profile, task_run,
                    context_pack, agent_task, envelope, decision, plan):
            uow._session.add(row)
            await uow._session.flush()
        await uow.commit()

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.bars.publish_series_revision(
            _bars_revision(security_id)
        ) is True
        await uow.commit()

    first = await MaturePerformanceService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW
    ).execute(as_of=NOW)
    assert first["matured_count"] > 0

    # 次日重跑：clock 前移 → known_at 变化 → 内容哈希不同，但 attribution_id 相同
    later_clock = NOW + timedelta(days=1)
    rerun = await MaturePerformanceService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: later_clock
    ).execute(as_of=later_clock)
    assert rerun["matured_count"] == 0
    assert rerun["skipped_count"] >= first["matured_count"]
    await engine.dispose()
