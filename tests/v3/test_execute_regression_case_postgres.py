"""RC-06C Regression Case 真执行真实 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.execute_regression_case import ExecuteRegressionCaseService
from app.v3.domain.performance import RegressionCaseCreate
from app.v3.infrastructure.db.models import RegressionCaseExecutionModel
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from tests.v3.test_deterministic_replay_postgres import _revision, _seed_chain

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 8, 26, 8, tzinfo=timezone.utc)


async def _create_case(sessions, input_requirements, expected_invariants):
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        case_id = await uow.performance.add_regression_case(RegressionCaseCreate(
            name=f"case-{uuid4().hex[:8]}",
            strategy_version=f"case-{uuid4().hex[:8]}",
            replay_as_of=NOW,
            input_requirements=input_requirements,
            expected_invariants=expected_invariants,
        ))
        await uow.commit()
    return case_id


async def test_regression_case_passes_with_pinned_inputs() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    revision = _revision(uuid4())
    pack_id, _ = await _seed_chain(sessions, revision)
    case_id = await _create_case(
        sessions,
        {"bar_revision_ids": [str(revision.revision_id)],
         "context_pack_ids": [str(pack_id)]},
        {"no_lookahead": True, "server_deterministic_executed": True,
         "feature_recompute_no_mismatch": True},
    )
    service = ExecuteRegressionCaseService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW,
    )
    report = await service.execute(case_id)
    assert report["status"] == "PASS"
    assert report["blocked_reason"] is None
    assert all(item["passed"] for item in report["invariant_results"].values())
    assert report["diff"]["feature_recompute"]["matched_count"] == 1

    # 执行记录 append-only 落库
    async with sessions() as session:
        rows = (await session.scalars(
            select(RegressionCaseExecutionModel).where(
                RegressionCaseExecutionModel.regression_case_id == case_id
            )
        )).all()
    assert len(rows) == 1
    assert rows[0].status == "PASS"
    assert rows[0].replay_run_id == report["replay_run_id"]
    await engine.dispose()


async def test_regression_case_with_missing_point_in_time_material_stays_blocked() -> None:
    """601233 语义：缺少原时点资料 → 继续 BLOCKED，绝不补造数据。"""
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    missing_revision = uuid4()
    case_id = await _create_case(
        sessions,
        {"bar_revision_ids": [str(missing_revision)]},
        {"no_lookahead": True},
    )
    service = ExecuteRegressionCaseService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW,
    )
    report = await service.execute(case_id)
    assert report["status"] == "BLOCKED"
    assert report["blocked_reason"] == "SOURCE_REPLAY_BLOCKED"
    assert report["diff"]["leakage_checks"][0]["reason"] == "MISSING_INPUT"

    async with sessions() as session:
        rows = (await session.scalars(
            select(RegressionCaseExecutionModel).where(
                RegressionCaseExecutionModel.regression_case_id == case_id
            )
        )).all()
    assert len(rows) == 1
    assert rows[0].status == "BLOCKED"
    await engine.dispose()
