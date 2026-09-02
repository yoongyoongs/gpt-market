from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.run_full_market_features import RunFullMarketFeaturesService
from app.v3.domain.features import FeatureQuery
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
    # artifacts 预加载包含全部幂等成功的上游（job-a 与 job-c）
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


def _make_weekly_revision(security_id) -> BarSeriesRevision:
    closes = [10 + index * 0.2 + (index % 3) * 0.05 for index in range(40)]
    bars = tuple(
        MarketBar(
            bar_time=NOW - timedelta(weeks=40 - index),
            open=close * 0.99, high=close * 1.01, low=close * 0.98,
            close=close, volume=10_000, amount=200_000,
            fetch_time=NOW - timedelta(minutes=1),
        )
        for index, close in enumerate(closes)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=security_id, period=BarPeriod.WEEK,
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
    index_revision = IndexBenchmarkRevision.build(IndexBenchmarkRevisionContent(
        revision_id=uuid4(), benchmark_code="HS300", source="fixture",
        upstream_source="fixture",
        fetch_time=NOW - timedelta(seconds=1), known_at=NOW - timedelta(seconds=1),
        bars=tuple(
            IndexBenchmarkBar(
                bar_time=NOW - timedelta(days=60 - index),
                close=3800 + index * 5 + (index % 4), amount=1e11,
            )
            for index in range(60)
        ),
    ))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(snapshot) is True
        assert await uow.index_benchmarks.publish(index_revision) is True
        await uow.commit()
        targets = await uow.universes.targets(snapshot.snapshot_id)
        for seed, target in enumerate(targets):
            assert await uow.bars.publish_series_revision(
                _make_revision(target.security_id, seed)
            ) is True
            assert await uow.bars.publish_series_revision(
                _make_weekly_revision(target.security_id)
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
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        page = await uow.features.query(FeatureQuery(
            feature_run_id=UUID(by_job["features"]["metrics"]["feature_run_id"]),
            fields=("features", "relative_index_strength"), limit=5,
        ))
    assert page is not None and len(page.items) == 2
    assert all(
        item["features"]["weekly_trend_state"] == "UP" for item in page.items
    )
    # RC-04-02：指数基准 20 日收益已注入，相对指数强度可计算
    assert all(
        item["relative_index_strength"] is not None for item in page.items
    )

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


@pytest.mark.asyncio
async def test_downstream_reruns_after_upstream_deduped_success() -> None:
    """生产缺陷回归：上游以 ALREADY_SUCCEEDED 幂等去重（本次 SKIPPED）时，
    下游绝不能被判 DEPENDENCY_FAILED 卡死 —— 真实生产曾把 index-benchmarks
    永久卡在 attempt 5。场景：首跑 job-a 成功、job-b 失败；重跑 job-a 被
    去重跳过，job-b 必须被允许重试。"""
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    trace: list[str] = []
    jobs = _jobs_with_trace(trace, fail={"job-b"})
    orchestrator = Orchestrator(lambda: SQLAlchemyUnitOfWork(sessions), jobs)
    first = await orchestrator.execute(trade_date=_trade_date(5), as_of=NOW)
    assert first["status"] == "PARTIAL"
    assert trace == ["job-a", "job-b", "job-c"]

    trace.clear()
    jobs_ok = _jobs_with_trace(trace)
    orchestrator_ok = Orchestrator(
        lambda: SQLAlchemyUnitOfWork(sessions), jobs_ok
    )
    second = await orchestrator_ok.execute(trade_date=_trade_date(5), as_of=NOW)
    await engine.dispose()
    assert second["status"] == "COMPLETED"
    by_job = {item["job_id"]: item for item in second["jobs"]}
    # job-a 幂等去重；job-b 必须真执行成功而不是 DEPENDENCY_FAILED
    assert by_job["job-a"]["error_type"] == "ALREADY_SUCCEEDED"
    assert by_job["job-b"]["status"] == "SUCCEEDED"
    # artifacts 预加载包含全部幂等成功的上游（job-a 与 job-c）
    assert by_job["job-b"]["metrics"]["upstream"] == ["job-a", "job-c"]


@pytest.mark.asyncio
async def test_latest_succeeded_idempotency_key_returns_terminal_key() -> None:
    """RT-05 catch-up：按 Job 取最近一次成功运行的幂等键（交易日）。

    使用专属 job_id，避免与其它测试共享的 job-a/b/c 幂等记录相互污染。
    """
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def handler(context) -> dict:
        return {"ok": True}

    jobs = (
        JobDefinition(job_id="catchup-probe", handler=handler),
    )
    orchestrator = Orchestrator(lambda: SQLAlchemyUnitOfWork(sessions), jobs)
    # 先成功跑 8/28，再成功跑 8/29
    await orchestrator.execute(trade_date=_trade_date(0), as_of=NOW)
    await orchestrator.execute(trade_date=_trade_date(1), as_of=NOW)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        latest = await uow.orchestrator.latest_succeeded_idempotency_key(
            "catchup-probe"
        )
        missing = await uow.orchestrator.latest_succeeded_idempotency_key(
            "no-such-job"
        )
    await engine.dispose()
    assert latest == _trade_date(1).isoformat()
    assert missing is None


@pytest.mark.asyncio
async def test_latest_runs_returns_recent_runs() -> None:
    """RT-08：流水线状态聚合依赖 latest_runs（known_at 降序，含 metrics）。"""
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    known_a = datetime.now(timezone.utc) - timedelta(seconds=2)
    known_b = datetime.now(timezone.utc)
    nonce = uuid4().hex[:8]  # 共享 DB：幂等键每次运行唯一，避免残留行冲突
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.orchestrator.record(
            orchestrator_run_id=uuid4(), job_id="eod-latest-probe",
            idempotency_key=f"rt08-{nonce}-1", attempt=1, status="SUCCEEDED",
            known_at=known_a, as_of=known_a, started_at=known_a,
            completed_at=known_a, error_type=None, error_summary=None,
            metrics={"n": 1},
        )
        await uow.orchestrator.record(
            orchestrator_run_id=uuid4(), job_id="eod-latest-probe",
            idempotency_key=f"rt08-{nonce}-2", attempt=1, status="FAILED",
            known_at=known_b, as_of=known_b, started_at=known_b,
            completed_at=known_b, error_type="ValueError", error_summary="boom",
            metrics={"n": 2},
        )
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        runs = await uow.orchestrator.latest_runs(limit=200)
    probe = [r for r in runs if r["idempotency_key"].startswith(f"rt08-{nonce}")]
    assert len(probe) == 2
    assert probe[0]["idempotency_key"] == f"rt08-{nonce}-2"  # known_at 降序
    assert probe[0]["error_summary"] == "boom"
    assert probe[1]["metrics"] == {"n": 1}
