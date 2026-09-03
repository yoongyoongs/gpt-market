"""RC-07A Shadow Runtime 真实 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.shadow_executor import ShadowExecutorService
from app.v3.domain.strategy import (
    ExperimentEventCommand,
    ActorType,
    GuardrailVersionCreate,
    StrategyExperimentCreate,
    StrategyVersionCreate,
)
from app.v3.infrastructure.db.models import ShadowObservationModel
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


async def _seed_experiment(sessions, *, experiment_type="SHADOW", allocation_percent=0):
    control_id = treatment_id = None
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        guardrail_id = await uow.strategies.add_guardrail(GuardrailVersionCreate(
            guardrail_code=f"gr-{uuid4().hex[:8]}", version=1,
            max_error_rate=Decimal("0.05"), max_p95_ms=Decimal("2000"),
            min_shadow_sample_count=1, max_divergence_rate=Decimal("0.10"),
            max_capacity_utilization=Decimal("0.8"), created_by="seed",
        ))
        await uow._session.flush()
        async def _version():
            version_id = uuid4()
            await uow.strategies.add_strategy_version(StrategyVersionCreate(
                strategy_version_id=version_id,
                strategy_code=f"strat-{uuid4().hex[:8]}", version=1,
                configuration={"horizons": [1, 3, 5]}, rationale="shadow seed",
                created_by="seed",
            ))
            await uow._session.flush()
            return version_id
        control_id = await _version()
        treatment_id = await _version()
        experiment_id = uuid4()
        await uow.strategies.add_experiment(StrategyExperimentCreate(
            experiment_id=experiment_id,
            experiment_type=experiment_type,
            control_strategy_version_id=control_id,
            treatment_strategy_version_id=treatment_id,
            guardrail_version_id=guardrail_id,
            allocation_percent=allocation_percent,
            starts_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            created_by="seed",
        ))
        await uow.strategies.append_experiment_event(experiment_id, ExperimentEventCommand(
            event_type="STARTED", actor_type=ActorType.HUMAN,
            actor_id="seed", reason="shadow run",
        ))
        await uow.commit()
    return experiment_id, control_id, treatment_id


async def test_active_experiments_returns_started_experiments() -> None:
    """回归：latest_event 子查询必须真实参与 join，否则生产报
    ProgrammingError: missing FROM-clause entry for table "anon_1"。"""
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    experiment_id, _, _ = await _seed_experiment(sessions)

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        rows = await uow.strategies.active_experiments(as_of=NOW)

    ids = {row["experiment_id"] for row in rows}
    assert experiment_id in ids
    await engine.dispose()


async def test_shadow_observation_persisted_with_real_repositories() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    experiment_id, control_id, treatment_id = await _seed_experiment(sessions)

    async def control(subject_key, as_of):
        return {"rank": 2, "score": 0.71}

    async def treatment(subject_key, as_of):
        return {"rank": 3, "score": 0.71}

    service = ShadowExecutorService(
        lambda: SQLAlchemyUnitOfWork(sessions),
        executors={control_id: control, treatment_id: treatment},
        clock=lambda: NOW,
    )
    report = await service.execute(experiment_id, f"SH:{uuid4().hex[:6]}")
    assert report["materially_divergent"] is True
    assert report["diff"]["paths"] == ["rank"]

    async with sessions() as session:
        rows = (await session.scalars(select(ShadowObservationModel).where(
            ShadowObservationModel.experiment_id == experiment_id
        ))).all()
    assert len(rows) == 1
    assert rows[0].materially_divergent is True
    assert "rank" in rows[0].divergence_reason
    assert rows[0].control_payload == {"rank": 2, "score": 0.71}
    await engine.dispose()
