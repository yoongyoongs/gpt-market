from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.run_full_market_features import RunFullMarketFeaturesService
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    Market,
    MarketBar,
    PointInTimePrecision,
    SecurityMember,
    UniverseSnapshot,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)
from app.v3.infrastructure.db.models import (
    FeatureRunModel,
    OrchestratorJobRunModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.jobs.orchestrator import (
    JobDefinition,
    Orchestrator,
    UnknownDependencyError,
)


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


def _jobs_with_trace(trace: list[str], fail: set[str] = frozenset()):
    def make(job_id: str, depends_on: tuple[str, ...]) -> JobDefinition:
        async def handler(context) -> dict:
            trace.append(job_id)
            if job_id in fail:
                raise RuntimeError(f"job {job_id} exploded")
            return {"upstream": sorted(context.artifacts), "job": job_id}

        return JobDefinition(job_id=job_id, handler=handler, depends_on=depends_on)

    return (
        make("job-a", ()),
        make("job-b", ("job-a",)),
        make("job-c", ()),
    )


def _trade_date(day_offset: int):
    return (NOW + timedelta(days=day_offset)).date()


async def _run_rows(sessions, idempotency_key: str | None = None):
    filters = []
    if idempotency_key is not None:
        filters.append(OrchestratorJobRunModel.idempotency_key == idempotency_key)
    async with sessions() as session:
        return (
            await session.scalars(
                select(OrchestratorJobRunModel)
                .where(*filters)
                .order_by(
                    OrchestratorJobRunModel.known_at,
                    OrchestratorJobRunModel.job_id,
                )
            )
        ).all()


@pytest.mark.asyncio
async def test_orchestrator_records_runs_and_passes_artifacts() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    trace: list[str] = []
    orchestrator = Orchestrator(lambda: SQLAlchemyUnitOfWork(sessions), _jobs_with_trace(trace))
    report = await orchestrator.execute(trade_date=_trade_date(1), as_of=NOW)
    await engine.dispose()
    assert report["status"] == "COMPLETED"
    assert trace == ["job-a", "job-b", "job-c"]
    by_job = {item["job_id"]: item for item in report["jobs"]}
    assert by_job["job-b"]["metrics"]["upstream"] == ["job-a"]
    rows = await _run_rows(sessions, _trade_date(1).isoformat())
    assert len(rows) == 3
    assert all(row.status == "SUCCEEDED" for row in rows)
    assert all(row.attempt == 1 for row in rows)
    assert all(row.as_of == NOW for row in rows)
    b_row = next(row for row in rows if row.job_id == "job-b")
    assert b_row.metrics["upstream"] == ["job-a"]


@pytest.mark.asyncio
async def test_orchestrator_second_run_is_idempotent() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    trace: list[str] = []
    orchestrator = Orchestrator(lambda: SQLAlchemyUnitOfWork(sessions), _jobs_with_trace(trace))
    first = await orchestrator.execute(trade_date=_trade_date(2), as_of=NOW)
    second = await orchestrator.execute(trade_date=_trade_date(2), as_of=NOW)
    await engine.dispose()
    assert first["status"] == "COMPLETED"
    assert second["status"] == "COMPLETED"
    assert trace == ["job-a", "job-b", "job-c"]
    by_job = {item["job_id"]: item for item in second["jobs"]}
    assert all(
        item["error_type"] == "ALREADY_SUCCEEDED"
        for item in by_job.values()
    )
    rows = await _run_rows(sessions, _trade_date(2).isoformat())
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_orchestrator_failure_skips_dependents_keeps_branches() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    trace: list[str] = []
    orchestrator = Orchestrator(
        lambda: SQLAlchemyUnitOfWork(sessions),
        _jobs_with_trace(trace, fail={"job-a"}),
    )
    report = await orchestrator.execute(trade_date=_trade_date(3), as_of=NOW)
    await engine.dispose()
    assert report["status"] == "PARTIAL"
    by_job = {item["job_id"]: item for item in report["jobs"]}
    assert by_job["job-a"]["status"] == "FAILED"
    assert by_job["job-a"]["error_type"] == "RuntimeError"
    assert by_job["job-b"]["status"] == "SKIPPED"
    assert by_job["job-b"]["error_type"] == "DEPENDENCY_FAILED"
    assert by_job["job-c"]["status"] == "SUCCEEDED"
    assert trace == ["job-a", "job-c"]


def test_orchestrator_graph_validation() -> None:
    def handler(context) -> dict:
        return {}

    with pytest.raises(UnknownDependencyError):
        Orchestrator(None, (JobDefinition("a", handler, ("missing",)),))
    with pytest.raises(ValueError):
        Orchestrator(None, (
            JobDefinition("a", handler, ("b",)),
            JobDefinition("b", handler, ("a",)),
        ))
    with pytest.raises(ValueError):
        Orchestrator(None, (JobDefinition("a", handler), JobDefinition("a", handler)))
    linear = Orchestrator(None, (
        JobDefinition("b", handler, ("a",)),
        JobDefinition("a", handler),
        JobDefinition("c", handler, ("b",)),
    ))
    assert linear.execution_order() == ("a", "b", "c")


def _make_revision(security_id, seed: int) -> BarSeriesRevision:
    bars = tuple(
        MarketBar(
            bar_time=NOW - timedelta(days=260 - index),
            open=10 + seed + index / 100,
            high=10.5 + seed + index / 100,
            low=9.5 + seed + index / 100,
            close=10.2 + seed + index / 100,
            volume=1000 + index,
            amount=2_000_000 + seed * 100_000 + index * 1000,
            fetch_time=NOW - timedelta(minutes=1),
        )
        for index in range(260)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=security_id, period=BarPeriod.DAY,
        adjust_type=AdjustType.QFQ, source="orchestrator-fixture",
        upstream_source="fixture", raw_bar_available=False,
        point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="fixture QFQ only",
        known_at=NOW - timedelta(seconds=1), bars=bars,
    ))


@pytest.mark.asyncio
async def test_feature_pipeline_published_and_second_run_zero_duplicate() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    snapshot = UniverseSnapshot.build(UniverseSnapshotContent(
        snapshot_id=uuid4(), source_code=f"orch-{uuid4().hex}",
        status=UniverseSnapshotStatus.PRIMARY, as_of=NOW - timedelta(minutes=2),
        fetch_time=NOW - timedelta(minutes=2), known_at=NOW - timedelta(minutes=2),
        coverage=1.0, stale=False,
        members=(
            SecurityMember(code="600011", market=Market.SH, name="pipe one"),
            SecurityMember(code="000012", market=Market.SZ, name="pipe two"),
        ),
    ))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(snapshot) is True
        await uow.commit()
        targets = await uow.universes.targets(snapshot.snapshot_id)
        for seed, target in enumerate(targets):
            assert await uow.bars.publish_series_revision(
                _make_revision(target.security_id, seed)
            ) is True
        await uow.commit()

    features_service = RunFullMarketFeaturesService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW
    )

    async def universe_handler(context) -> dict:
        return {"universe_snapshot_id": str(snapshot.snapshot_id)}

    async def features_handler(context) -> dict:
        upstream = context.artifact("universe")
        run = await features_service.execute(
            universe_snapshot_id=UUID(upstream["universe_snapshot_id"]),
            as_of=context.as_of,
            feature_version=f"orchestrator-{context.trade_date.isoformat()}-{uuid4().hex[:8]}",
        )
        return {
            "feature_run_id": str(run.feature_run_id),
            "status": run.status.value,
            "successful_count": run.successful_count,
        }

    jobs = (
        JobDefinition(job_id="universe", handler=universe_handler),
        JobDefinition(job_id="features", handler=features_handler,
                      depends_on=("universe",)),
    )
    orchestrator = Orchestrator(lambda: SQLAlchemyUnitOfWork(sessions), jobs)
    report = await orchestrator.execute(trade_date=_trade_date(4), as_of=NOW)
    by_job = {item["job_id"]: item for item in report["jobs"]}
    assert report["status"] == "COMPLETED"
    assert by_job["features"]["metrics"]["status"] == "PUBLISHED"
    assert by_job["features"]["metrics"]["successful_count"] == 2
    # 第二次运行：特征 Run 零重复，Job 记录幂等跳过
    second = await orchestrator.execute(trade_date=_trade_date(4), as_of=NOW)
    await engine.dispose()
    assert second["status"] == "COMPLETED"
    by_job = {item["job_id"]: item for item in second["jobs"]}
    assert by_job["universe"]["error_type"] == "ALREADY_SUCCEEDED"
    assert by_job["features"]["error_type"] == "ALREADY_SUCCEEDED"
    async with sessions() as session:
        count = (
            await session.scalar(
                select(func.count()).select_from(FeatureRunModel).where(
                    FeatureRunModel.feature_version.like("orchestrator-%")
                )
            )
        )
    assert count == 1
