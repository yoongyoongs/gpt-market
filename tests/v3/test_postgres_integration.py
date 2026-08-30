from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.aggregate_daily_bars import AggregateDailyBarsService
from app.v3.application.backfill_daily_bars import BackfillDailyBarsService
from app.v3.application.ingest_daily_bars import BuildDailyBarRevisionsService
from app.v3.application.ingest_corporate_actions import IngestCorporateActionsService
from app.v3.application.publish_bar_bundle import PublishBarBundleService
from app.v3.application.register_agent_task import RegisterAgentTaskService
from app.v3.contracts.agent import AgentTask, Subject, SubjectType
from app.v3.domain.market_data import (
    BarIngestionTarget,
    IngestionRunStatus,
    Market,
    CorporateActionDraft,
    CorporateActionFetchResult,
    CorporateActionType,
    SecurityMember,
    UniverseSnapshot,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.infrastructure.db.session import V3Database
from app.utils.time import SHANGHAI
from tests.v3.test_backfill_daily_bars import AlwaysOpenCalendar, DynamicProvider
from tests.v3.test_ingest_daily_bars import FakeProvider, NOW


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_market_job_advisory_lock_prevents_overlap() -> None:
    assert DATABASE_URL is not None
    lock_key = uuid4().int % 2_000_000_000
    first = V3Database(DATABASE_URL, echo=False, pool_size=1, max_overflow=0)
    second = V3Database(DATABASE_URL, echo=False, pool_size=1, max_overflow=0)
    await first.acquire_advisory_lock(lock_key)

    with pytest.raises(RuntimeError, match="already held"):
        await second.acquire_advisory_lock(lock_key)

    await first.close()
    await second.acquire_advisory_lock(lock_key)
    await second.close()


@pytest.mark.asyncio
async def test_daily_coverage_compares_shanghai_trading_date() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    security_id = uuid4()
    qfq_revision_id = uuid4()
    hfq_revision_id = uuid4()
    trading_date = date(2026, 8, 28)
    bar_time = datetime.combine(trading_date, datetime.min.time(), tzinfo=SHANGHAI)
    known_at = bar_time + timedelta(hours=16)
    target = BarIngestionTarget(
        security_id=security_id, code=f"{security_id.int % 1_000_000:06d}", market=Market.SH
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.securities (security_id, code, market, name) "
                "VALUES (:security_id, :code, 'SH', 'timezone fixture')"
            ),
            {"security_id": security_id, "code": target.code},
        )
        await connection.execute(
            text(
                "INSERT INTO v3.bar_series_revisions "
                "(revision_id, security_id, period, adjust_type, source, upstream_source, "
                "raw_bar_available, point_in_time_precision, known_at, content_hash) "
                "VALUES (:revision_id, :security_id, 'DAY', :adjust_type, 'fixture', 'fixture', "
                "true, 'FULL', :known_at, :content_hash)"
            ),
            {
                "revision_id": qfq_revision_id,
                "security_id": security_id,
                "adjust_type": "QFQ",
                "known_at": known_at,
                "content_hash": qfq_revision_id.hex * 2,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO v3.market_bars "
                "(revision_id, bar_time, open, high, low, close, volume, amount, provisional, "
                "event_time, fetch_time) VALUES "
                "(:revision_id, :bar_time, 10, 11, 9, 10, 100, 1000, false, :bar_time, :known_at)"
            ),
            {
                "revision_id": qfq_revision_id,
                "bar_time": bar_time,
                "known_at": known_at,
            },
        )

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        qfq_only_covered = await uow.bars.covered_daily_security_ids(
            (target,), minimum_bars=1, minimum_last_bar_date=trading_date
        )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.bar_series_revisions "
                "(revision_id, security_id, period, adjust_type, source, upstream_source, "
                "raw_bar_available, point_in_time_precision, known_at, content_hash) "
                "VALUES (:revision_id, :security_id, 'DAY', 'HFQ', 'fixture', 'fixture', "
                "true, 'FULL', :known_at, :content_hash)"
            ),
            {
                "revision_id": hfq_revision_id,
                "security_id": security_id,
                "known_at": known_at,
                "content_hash": hfq_revision_id.hex * 2,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO v3.market_bars "
                "(revision_id, bar_time, open, high, low, close, volume, amount, provisional, "
                "event_time, fetch_time) VALUES "
                "(:revision_id, :bar_time, 10, 11, 9, 10, 100, 1000, false, :bar_time, :known_at)"
            ),
            {
                "revision_id": hfq_revision_id,
                "bar_time": bar_time,
                "known_at": known_at,
            },
        )

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        covered = await uow.bars.covered_daily_security_ids(
            (target,), minimum_bars=1, minimum_last_bar_date=trading_date
        )
        individually_covered = await uow.bars.has_daily_coverage(
            security_id, minimum_bars=1, minimum_last_bar_date=trading_date
        )
    await engine.dispose()

    assert qfq_only_covered == set()
    assert covered == {security_id}
    assert individually_covered is True


@pytest.fixture
async def connection():
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as value:
        yield value
        await value.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_phase1_schema_and_immutable_raw_document(connection) -> None:
    table_names = {
        row[0]
        for row in (
            await connection.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='v3'")
            )
        )
    }
    assert {
        "evidence_sources",
        "raw_documents",
        "evidence_records",
        "task_profiles",
        "expected_runs",
        "task_runs",
        "agent_tasks",
        "ai_result_envelopes",
        "audit_events",
    } <= table_names

    source_id = uuid4()
    document_id = uuid4()
    now = datetime.now(timezone.utc)
    await connection.execute(
        text(
            "INSERT INTO v3.evidence_sources "
            "(evidence_source_id, code, source_type) VALUES (:id, :code, 'OFFICIAL')"
        ),
        {"id": source_id, "code": f"fixture-{source_id}"},
    )
    await connection.execute(
        text(
            "INSERT INTO v3.raw_documents "
            "(raw_document_id, evidence_source_id, raw_reference, document_key, "
            "normalized_reference, fetch_time, known_at, content_hash) "
            "VALUES (:id, :source_id, 'fixture', :document_key, 'https://example.com/fixture', "
            ":fetch_time, :known_at, :content_hash)"
        ),
        {
            "id": document_id,
            "source_id": source_id,
            "document_key": f"fixture-{document_id}",
            "fetch_time": now,
            "known_at": now,
            "content_hash": document_id.hex + document_id.hex,
        },
    )
    await connection.commit()

    with pytest.raises(DBAPIError, match="immutable V3 record"):
        await connection.execute(
            text("UPDATE v3.raw_documents SET raw_reference='changed' WHERE raw_document_id=:id"),
            {"id": document_id},
        )
    await connection.rollback()

@pytest.mark.asyncio
async def test_task_group_count_constraint_rejects_inconsistent_state(connection) -> None:
    profile_id = uuid4()
    await connection.execute(
        text(
            "INSERT INTO v3.task_profiles "
            "(task_profile_id, profile_code, version, timezone, context_level, output_schema, content_hash) "
            "VALUES (:id, :code, 1, 'Asia/Shanghai', 'NORMAL', '{}'::jsonb, :content_hash)"
        ),
        {"id": profile_id, "code": f"fixture-{profile_id}", "content_hash": profile_id.hex + profile_id.hex},
    )
    await connection.commit()

    with pytest.raises(IntegrityError, match="ck_task_runs_group_count_total"):
        await connection.execute(
            text(
                "INSERT INTO v3.task_runs "
                "(task_run_id, task_profile_id, expected_group_count, successful_group_count, "
                "failed_group_count, pending_group_count) VALUES (:id, :profile_id, 30, 29, 0, 0)"
            ),
            {"id": uuid4(), "profile_id": profile_id},
        )
    await connection.rollback()


@pytest.mark.asyncio
async def test_agent_task_audit_commit_idempotency_rollback_and_concurrency() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    profile_id = uuid4()
    task_run_id = uuid4()
    now = datetime.now(timezone.utc)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.task_profiles "
                "(task_profile_id, profile_code, version, timezone, context_level, output_schema, content_hash) "
                "VALUES (:id, :code, 1, 'Asia/Shanghai', 'NORMAL', '{}'::jsonb, :content_hash)"
            ),
            {"id": profile_id, "code": f"uow-{profile_id}", "content_hash": profile_id.hex * 2},
        )
        await connection.execute(
            text(
                "INSERT INTO v3.task_runs "
                "(task_run_id, task_profile_id, expected_group_count, pending_group_count) "
                "VALUES (:id, :profile_id, 1, 1)"
            ),
            {"id": task_run_id, "profile_id": profile_id},
        )

    task = AgentTask(
        task_id=uuid4(),
        task_run_id=task_run_id,
        task_type="STOCK_REVIEW",
        subject=Subject(type=SubjectType.STOCK, code="600000"),
        task_profile="stock-review-v1",
        trigger_type="INTEGRATION_TEST",
        as_of=now,
        context_pack_id=uuid4(),
        context_pack_hash="a" * 64,
        expected_result_type="STOCK_REVIEW",
        constraints={"correlation_id": uuid4()},
    )
    service = RegisterAgentTaskService(lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: now)

    first = await service.execute(task, actor_type="SYSTEM", request_id=f"test-{task.task_id}")
    second = await service.execute(task, actor_type="SYSTEM", request_id=f"test-{task.task_id}")
    assert first.created is True
    assert second.created is False

    rejected_task = task.model_copy(update={"task_id": uuid4()})
    with pytest.raises(ValidationError, match="actor_type"):
        await service.execute(rejected_task, actor_type="x" * 33)

    conflicting_task = task.model_copy(update={"task_type": "DIFFERENT_REVIEW"})
    with pytest.raises(IntegrityError):
        await service.execute(conflicting_task, actor_type="SYSTEM")

    concurrent_task = task.model_copy(update={"task_id": uuid4(), "context_pack_id": uuid4()})
    concurrent_results = await asyncio.gather(
        *(
            service.execute(concurrent_task, actor_type="SYSTEM", request_id=f"concurrent-{index}")
            for index in range(8)
        )
    )
    assert sum(result.created for result in concurrent_results) == 1

    async with engine.connect() as connection:
        task_count = (
            await connection.execute(
                text("SELECT count(*) FROM v3.agent_tasks WHERE task_run_id=:task_run_id"),
                {"task_run_id": task_run_id},
            )
        ).scalar_one()
        audit_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM v3.audit_events "
                    "WHERE object_type='AGENT_TASK' AND object_id=:object_id"
                ),
                {"object_id": str(task.task_id)},
            )
        ).scalar_one()
        concurrent_audit_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM v3.audit_events "
                    "WHERE object_type='AGENT_TASK' AND object_id=:object_id"
                ),
                {"object_id": str(concurrent_task.task_id)},
            )
        ).scalar_one()
    await engine.dispose()

    assert task_count == 2
    assert audit_count == 1
    assert concurrent_audit_count == 1


@pytest.mark.asyncio
async def test_phase2_schema_constraints_and_immutable_market_bars(connection) -> None:
    table_names = {
        row[0]
        for row in (
            await connection.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='v3'")
            )
        )
    }
    assert {
        "securities",
        "universe_sources",
        "universe_snapshots",
        "universe_members",
        "universe_diffs",
        "market_data_ingestion_runs",
        "adjustment_factor_revisions",
        "adjustment_factors",
        "bar_series_revisions",
        "market_bars",
        "corporate_actions",
    } <= table_names

    source_id = uuid4()
    security_id = uuid4()
    revision_id = uuid4()
    now = datetime.now(timezone.utc)
    await connection.execute(
        text(
            "INSERT INTO v3.universe_sources "
            "(source_id, code, source_type, priority, capability_version) "
            "VALUES (:id, :code, 'VENDOR', 1, 'v1')"
        ),
        {"id": source_id, "code": f"phase2-{source_id}"},
    )
    await connection.commit()

    with pytest.raises(IntegrityError, match="lkg_requires_stale"):
        snapshot_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO v3.universe_snapshots "
                "(snapshot_id, source_id, as_of, fetch_time, known_at, coverage, stale, content_hash, status) "
                "VALUES (:id, :source_id, :now, :now, :now, 1, false, :hash, 'LKG')"
            ),
            {"id": snapshot_id, "source_id": source_id, "now": now, "hash": snapshot_id.hex * 2},
        )
    await connection.rollback()


    await connection.execute(
        text("INSERT INTO v3.securities (security_id, code, market, name) VALUES (:id, :code, 'SH', 'fixture')"),
        {"id": security_id, "code": f"{security_id.int % 1_000_000:06d}"},
    )
    await connection.execute(
        text(
            "INSERT INTO v3.bar_series_revisions "
            "(revision_id, security_id, period, adjust_type, source, upstream_source, raw_bar_available, "
            "point_in_time_precision, known_at, content_hash) "
            "VALUES (:id, :security_id, 'DAY', 'RAW', 'fixture', 'eastmoney', true, 'FULL', :now, :hash)"
        ),
        {"id": revision_id, "security_id": security_id, "now": now, "hash": revision_id.hex * 2},
    )
    await connection.execute(
        text(
            "INSERT INTO v3.market_bars "
            "(revision_id, bar_time, open, high, low, close, volume, amount, provisional, event_time, fetch_time) "
            "VALUES (:revision_id, :now, 10, 11, 9, 10.5, 100, 1000, false, :now, :now)"
        ),
        {"revision_id": revision_id, "now": now},
    )
    await connection.execute(
        text(
            "INSERT INTO v3.market_bars "
            "(revision_id, bar_time, open, high, low, close, volume, amount, provisional, event_time, fetch_time) "
            "VALUES (:revision_id, :bar_time, 10, 11, 9, 10.5, 100, NULL, false, :bar_time, :now)"
        ),
        {"revision_id": revision_id, "bar_time": now - timedelta(days=2), "now": now},
    )
    await connection.commit()

    with pytest.raises(DBAPIError, match="immutable V3 record"):
        await connection.execute(
            text("UPDATE v3.market_bars SET close=10.6 WHERE revision_id=:revision_id"),
            {"revision_id": revision_id},
        )
    await connection.rollback()

    with pytest.raises(IntegrityError, match="valid_high"):
        await connection.execute(
            text(
                "INSERT INTO v3.market_bars "
                "(revision_id, bar_time, open, high, low, close, volume, amount, provisional, event_time, fetch_time) "
                "VALUES (:revision_id, :bar_time, 10, 9, 8, 10, 100, 1000, false, :bar_time, :now)"
            ),
            {"revision_id": revision_id, "bar_time": now - timedelta(days=1), "now": now},
        )
    await connection.rollback()


@pytest.mark.asyncio
async def test_universe_repository_publishes_latest_and_diffs_atomically() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    seed = uuid4().int % 800_000

    def member(offset: int, name: str) -> SecurityMember:
        return SecurityMember(
            code=f"{(seed + offset) % 1_000_000:06d}",
            market=Market.SH,
            name=name,
        )

    first = UniverseSnapshot.build(
        UniverseSnapshotContent(
            snapshot_id=uuid4(),
            source_code=f"integration-{uuid4()}",
            status=UniverseSnapshotStatus.PRIMARY,
            as_of=now,
            fetch_time=now,
            known_at=now,
            coverage=1,
            stale=False,
            members=(member(1, "甲"), member(2, "乙")),
        )
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(first) is True
        await uow.commit()

    later = now + timedelta(seconds=1)
    second = UniverseSnapshot.build(
        UniverseSnapshotContent(
            snapshot_id=uuid4(),
            source_code=first.source_code,
            status=UniverseSnapshotStatus.PRIMARY,
            as_of=later,
            fetch_time=later,
            known_at=later,
            coverage=1,
            stale=False,
            previous_snapshot_id=first.snapshot_id,
            members=(member(1, "甲更名"), member(3, "丙")),
        )
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(second) is True
        await uow.commit()

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        latest = await uow.universes.latest()
    assert latest == second

    async with engine.connect() as connection:
        diff_types = (
            await connection.execute(
                text(
                    "SELECT change_type, count(*) FROM v3.universe_diffs "
                    "WHERE snapshot_id=:snapshot_id GROUP BY change_type ORDER BY change_type"
                ),
                {"snapshot_id": second.snapshot_id},
            )
        ).all()
    await engine.dispose()
    assert diff_types == [("ADDED", 1), ("CHANGED", 1), ("REMOVED", 1)]


@pytest.mark.asyncio
async def test_bar_bundle_repository_is_atomic_and_idempotent() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    security_id = uuid4()
    code = f"{security_id.int % 1_000_000:06d}"
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.securities (security_id, code, market, name) "
                "VALUES (:security_id, :code, 'SH', 'bar fixture')"
            ),
            {"security_id": security_id, "code": code},
        )

    bundle = await BuildDailyBarRevisionsService(
        [FakeProvider("integration-bars")], clock=lambda: NOW
    ).execute(security_id, "600000")
    service = PublishBarBundleService(lambda: SQLAlchemyUnitOfWork(sessions))
    first = await service.execute(bundle)
    second = await service.execute(bundle)

    async with engine.connect() as connection:
        factor_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM v3.adjustment_factor_revisions "
                    "WHERE security_id=:security_id"
                ),
                {"security_id": security_id},
            )
        ).scalar_one()
        series_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM v3.bar_series_revisions "
                    "WHERE security_id=:security_id"
                ),
                {"security_id": security_id},
            )
        ).scalar_one()
        bar_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM v3.market_bars b "
                    "JOIN v3.bar_series_revisions r ON r.revision_id=b.revision_id "
                    "WHERE r.security_id=:security_id"
                ),
                {"security_id": security_id},
            )
        ).scalar_one()
    await engine.dispose()

    assert first.factor_created is True
    assert first.series_created == 3
    assert second.factor_created is False
    assert second.series_created == 0
    assert (factor_count, series_count, bar_count) == (1, 3, 9)


@pytest.mark.asyncio
async def test_bar_bundle_appends_supersedes_revision_chain() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    security_id = uuid4()
    code = f"{security_id.int % 1_000_000:06d}"
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.securities (security_id, code, market, name) "
                "VALUES (:security_id, :code, 'SH', 'revision fixture')"
            ),
            {"security_id": security_id, "code": code},
        )

    service = PublishBarBundleService(lambda: SQLAlchemyUnitOfWork(sessions))
    first = await BuildDailyBarRevisionsService(
        [DynamicProvider("revision-bars")], clock=lambda: NOW
    ).execute(security_id, code)
    second = await BuildDailyBarRevisionsService(
        [DynamicProvider("revision-bars")], clock=lambda: NOW + timedelta(seconds=1)
    ).execute(security_id, code)
    await service.execute(first)
    await service.execute(second)

    async with engine.connect() as connection:
        factor_links = (
            await connection.execute(
                text(
                    "SELECT factor_revision_id, supersedes_revision_id "
                    "FROM v3.adjustment_factor_revisions WHERE security_id=:security_id "
                    "ORDER BY known_at"
                ),
                {"security_id": security_id},
            )
        ).all()
        series_links = (
            await connection.execute(
                text(
                    "SELECT period, adjust_type, revision_id, supersedes_revision_id "
                    "FROM v3.bar_series_revisions WHERE security_id=:security_id "
                    "ORDER BY period, adjust_type, known_at"
                ),
                {"security_id": security_id},
            )
        ).all()
    await engine.dispose()

    assert len(factor_links) == 2
    assert factor_links[1].supersedes_revision_id == factor_links[0].factor_revision_id
    assert len(series_links) == 6
    for index in (0, 2, 4):
        assert series_links[index + 1].supersedes_revision_id == series_links[index].revision_id


@pytest.mark.asyncio
async def test_backfill_run_reads_universe_checkpoints_and_replays_without_duplicates() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc) + timedelta(seconds=2)
    code_seed = uuid4().int % 99_998
    codes = (f"6{code_seed:05d}", f"6{code_seed + 1:05d}")
    universe = UniverseSnapshot.build(
        UniverseSnapshotContent(
            snapshot_id=uuid4(),
            source_code=f"backfill-{uuid4()}",
            status=UniverseSnapshotStatus.PRIMARY,
            as_of=now,
            fetch_time=now,
            known_at=now,
            coverage=1,
            stale=False,
            members=(
                SecurityMember(code=codes[0], market=Market.SH, name="回填甲"),
                SecurityMember(code=codes[1], market=Market.SH, name="回填乙"),
            ),
        )
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(universe) is True
        await uow.commit()

    def uow_factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(sessions)
    publisher = PublishBarBundleService(uow_factory)
    provider = DynamicProvider("integration")
    runner = BackfillDailyBarsService(
        uow_factory,
        BuildDailyBarRevisionsService([provider], clock=lambda: NOW),
        AggregateDailyBarsService(AlwaysOpenCalendar(), clock=lambda: NOW),
        publisher,
        clock=lambda: now,
    )
    first = await runner.execute(minimum_last_bar_date=(NOW - timedelta(days=10)).date())
    second = await runner.execute(
        run_id=first.run_id,
        minimum_last_bar_date=(NOW - timedelta(days=10)).date(),
    )
    third = await runner.execute(minimum_last_bar_date=(NOW - timedelta(days=10)).date())

    async with engine.connect() as connection:
        run_counts = (
            await connection.execute(
                text(
                    "SELECT status, expected_count, processed_count, successful_count, failed_count "
                    "FROM v3.market_data_ingestion_runs WHERE run_id=:run_id"
                ),
                {"run_id": first.run_id},
            )
        ).one()
        series_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM v3.bar_series_revisions r "
                    "JOIN v3.universe_members um ON um.security_id=r.security_id "
                    "WHERE um.snapshot_id=:snapshot_id"
                ),
                {"snapshot_id": universe.snapshot_id},
            )
        ).scalar_one()
    await engine.dispose()

    assert first.status is IngestionRunStatus.COMPLETED
    assert second.status is IngestionRunStatus.COMPLETED
    assert third.status is IngestionRunStatus.COMPLETED
    assert provider.calls == 4
    assert tuple(run_counts) == ("COMPLETED", 2, 2, 2, 0)
    assert series_count == 12


@pytest.mark.asyncio
async def test_corporate_actions_are_idempotent_and_append_corrections() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc) + timedelta(seconds=2)
    code = f"6{uuid4().int % 99_999:05d}"
    universe = UniverseSnapshot.build(
        UniverseSnapshotContent(
            snapshot_id=uuid4(),
            source_code=f"corporate-{uuid4()}",
            status=UniverseSnapshotStatus.PRIMARY,
            as_of=now,
            fetch_time=now,
            known_at=now,
            coverage=1,
            stale=False,
            members=(SecurityMember(code=code, market=Market.SH, name="公司行动样本"),),
        )
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(universe) is True
        await uow.commit()

    class Provider:
        code = "integration-corporate"

        def __init__(self) -> None:
            self.plan = "10派1元"

        async def fetch_since(self, since):
            action = CorporateActionDraft(
                code=code,
                market=Market.SH,
                action_type=CorporateActionType.CASH_DIVIDEND,
                announcement_time=now - timedelta(days=10),
                record_time=now - timedelta(days=2),
                effective_time=now - timedelta(days=1),
                payload={"plan": self.plan, "cash_dividend_per_10_shares": 1.0},
                source=self.code,
                source_reference=f"integration://{code}/2025-12-31",
                fetch_time=now,
            )
            return CorporateActionFetchResult(
                source_code=self.code, fetch_time=now, actions=(action,)
            )

    provider = Provider()
    service = IngestCorporateActionsService(
        lambda: SQLAlchemyUnitOfWork(sessions), (provider,), clock=lambda: now
    )
    first = await service.execute(date(2025, 1, 1))
    replay = await service.execute(date(2025, 1, 1))
    provider.plan = "10派1.1元"
    correction = await service.execute(date(2025, 1, 1))

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT corporate_action_id, supersedes_action_id FROM v3.corporate_actions "
                    "WHERE source_reference=:reference ORDER BY known_at, created_at"
                ),
                {"reference": f"integration://{code}/2025-12-31"},
            )
        ).all()
    await engine.dispose()

    assert (first.published_count, replay.unchanged_count) == (1, 1)
    assert correction.published_count == 1
    assert len(rows) == 2
    assert rows[1].supersedes_action_id == rows[0].corporate_action_id
