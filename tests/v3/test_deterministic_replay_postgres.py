"""RC-06B Deterministic Replay Engine 真实 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.calculate_features import CalculateSecurityFeatureService
from app.v3.application.deterministic_replay import DeterministicReplayService
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    MarketBar,
    PointInTimePrecision,
)
from app.v3.domain.performance import ReplayRunCreate
from app.v3.infrastructure.db.models import (
    AIResultEnvelopeModel,
    AgentTaskModel,
    ContextPackModel,
    FeatureRunModel,
    ReplayRunModel,
    SecurityFeatureModel,
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


def _revision(security_id, *, known_at=NOW - timedelta(minutes=1)):
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=security_id,
        period=BarPeriod.DAY, adjust_type=AdjustType.QFQ,
        source="fixture", upstream_source="fixture",
        raw_bar_available=False,
        point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="fixture QFQ only",
        known_at=known_at,
        bars=tuple(
            MarketBar(
                bar_time=datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=index),
                open=10.0 + index * 0.1, high=10.2 + index * 0.1,
                low=9.8 + index * 0.1, close=10.0 + index * 0.1,
                volume=1_000_000, amount=1e7,
                fetch_time=NOW - timedelta(minutes=1),
            )
            for index in range(30)
        ),
    ))


async def _seed_chain(sessions, revision):
    """security + pinned revision + feature run + feature + context pack + envelope。"""
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        session = uow._session
        security = SecurityModel(security_id=revision.security_id,
                                 code=f"60{uuid4().hex[:4]}",
                                 market="SH", name="replay fixture")
        session.add(security)
        await session.flush()
        assert await uow.bars.publish_series_revision(revision) is True

        source = UniverseSourceModel(
            source_id=uuid4(), code=f"replay-{uuid4().hex[:12]}",
            source_type="EXCHANGE", priority=1, capability_version="1",
        )
        snapshot = UniverseSnapshotModel(
            snapshot_id=uuid4(), source_id=source.source_id, as_of=NOW,
            fetch_time=NOW, known_at=NOW, coverage=Decimal("1"),
            stale=False, status="PRIMARY", content_hash=uuid4().hex,
        )
        feature_run_id = uuid4()
        feature_run = FeatureRunModel(
            feature_run_id=feature_run_id, as_of=NOW,
            universe_snapshot_id=snapshot.snapshot_id, feature_version="v1",
            status="PUBLISHED", expected_count=1, coverage=Decimal("1"),
            bar_revision_set_hash="0" * 64, input_manifest={}, started_at=NOW,
        )
        for row in (source, snapshot, feature_run):
            session.add(row)
            await session.flush()

        # 用同一确定性引擎重算一次作为"当时落库"的 immutable Feature
        feature = CalculateSecurityFeatureService().execute(
            feature_run_id=feature_run_id, revision=revision, as_of=NOW,
        )
        payload = feature.model_dump(mode="json")
        payload.update({
            "feature_run_id": feature_run_id,
            "security_id": revision.security_id,
            "series_revision_id": revision.revision_id,
            "factor_revision_id": revision.factor_revision_id,
            "as_of": feature.as_of,
        })
        session.add(SecurityFeatureModel(**payload))
        await session.flush()

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
            subject_type="SECURITY", subject_id=f"SH:{security.code}",
            task_profile_id=profile.task_profile_id, task_profile_version=1,
            builder_version="v1", schema_version="v1", as_of=NOW, known_at=NOW,
            universe_snapshot_id=snapshot.snapshot_id,
            feature_run_id=feature_run_id,
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
            evidence_ids=[], payload={"direction": "LONG"},
            content_hash=uuid4().hex,
        )
        for row in (profile, task_run, context_pack, agent_task, envelope):
            session.add(row)
            await session.flush()
        await uow.commit()
    return context_pack.context_pack_id, envelope


async def test_replay_recomputes_verifies_and_records_ai_boundary() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    revision = _revision(uuid4())
    pack_id, envelope = await _seed_chain(sessions, revision)

    service = DeterministicReplayService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW,
    )
    command = ReplayRunCreate(
        strategy_version=f"replay-{uuid4().hex[:8]}", replay_as_of=NOW,
        bar_revision_ids=(revision.revision_id,), context_pack_ids=(pack_id,),
    )
    report = await service.execute(command)
    assert report["status"] == "COMPLETED"
    layers = report["result"]["layers"]
    deterministic = layers["server_deterministic"]
    assert deterministic["feature_recompute"]["recomputed_count"] == 1
    assert deterministic["feature_recompute"]["matched_count"] == 1
    assert deterministic["feature_recompute"]["mismatched"] == []
    ai_layer = layers["ai_decision_replay"]
    assert ai_layer["executed"] is False
    assert ai_layer["boundary"] == "SERVER_HAS_NO_MODEL_API"
    assert ai_layer["immutable_result_replay"]["available"] is True
    assert ai_layer["immutable_result_replay"]["result_id"] == str(envelope.result_id)

    # Replay 落库：status/result/layers 持久化
    async with sessions() as session:
        row = await session.get(ReplayRunModel, report["replay_run_id"])
    assert row is not None
    assert row.status == "COMPLETED"
    assert row.result["layers"]["ai_decision_replay"]["boundary"] == "SERVER_HAS_NO_MODEL_API"

    # Gate：泄漏 revision（known_at > replay_as_of）→ BLOCKED，两层都不执行
    leaked = _revision(revision.security_id, known_at=NOW + timedelta(minutes=1))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.bars.publish_series_revision(leaked)
        await uow.commit()
    leaked_command = ReplayRunCreate(
        strategy_version=f"replay-{uuid4().hex[:8]}", replay_as_of=NOW,
        bar_revision_ids=(leaked.revision_id,),
    )
    blocked = await service.execute(leaked_command)
    assert blocked["status"] == "BLOCKED"
    assert blocked["result"]["layers"]["server_deterministic"]["executed"] is False
    await engine.dispose()


async def test_missing_pinned_revision_blocks_replay() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = DeterministicReplayService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW,
    )
    command = ReplayRunCreate(
        strategy_version=f"replay-{uuid4().hex[:8]}", replay_as_of=NOW,
        bar_revision_ids=(uuid4(),),
    )
    report = await service.execute(command)
    assert report["status"] == "BLOCKED"
    assert report["leakage_checks"][0]["reason"] == "MISSING_INPUT"
    await engine.dispose()
