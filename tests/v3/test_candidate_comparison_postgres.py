from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.context import (
    CANDIDATE_COMPARISON_SCHEMA_VERSION,
    CandidateComparisonMember,
    CandidateComparisonPack,
)
from app.v3.domain.market_data import (
    Market,
    SecurityMember,
    UniverseSnapshot,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.repositories.errors import RepositoryConflictError, RepositoryNotFoundError


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
# Keep the fixture safely older than tests that intentionally query the latest snapshot.
NOW = datetime(2020, 1, 2, 8, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_phase6_schema_contains_complete_foundation_and_triggers() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        tables = set(
            (
                await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='v3'"
                    )
                )
            ).scalars()
        )
        assert {
            "candidate_comparison_packs",
            "candidate_comparison_members",
            "context_packs",
            "context_evidence_selections",
        } <= tables
        triggers = set(
            (
                await connection.execute(
                    text(
                        "SELECT event_object_table FROM information_schema.triggers "
                        "WHERE trigger_schema='v3' AND trigger_name='prevent_mutation'"
                    )
                )
            ).scalars()
        )
        assert {
            "candidate_comparison_packs",
            "candidate_comparison_members",
            "context_packs",
            "context_evidence_selections",
            "task_profiles",
        } <= triggers
        task_columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT table_name || '.' || column_name "
                        "FROM information_schema.columns WHERE table_schema='v3' "
                        "AND table_name IN ('task_profiles','expected_runs','task_runs')"
                    )
                )
            ).scalars()
        )
        assert {
            "task_profiles.trading_calendar_source",
            "task_profiles.trading_calendar_version",
            "task_profiles.comparison_first",
            "task_profiles.candidate_limit",
            "task_profiles.topk_limit",
            "task_profiles.topk_context_level",
            "task_profiles.strategy_version",
            "expected_runs.task_profile_version",
            "expected_runs.known_at",
            "expected_runs.content_hash",
            "expected_runs.row_version",
            "task_runs.task_profile_version",
        } <= task_columns
    await engine.dispose()


def _comparison_pack(
    *,
    universe_snapshot_id: UUID,
    feature_run_id: UUID,
    members: tuple[CandidateComparisonMember, ...],
    comparison_pack_id: UUID | None = None,
    builder_version: str = "comparison-persistence.v1",
    as_of: datetime = NOW,
) -> CandidateComparisonPack:
    return CandidateComparisonPack.build(
        comparison_pack_id=comparison_pack_id or uuid4(),
        candidate_set_id=uuid4(),
        builder_version=builder_version,
        schema_version=CANDIDATE_COMPARISON_SCHEMA_VERSION,
        field_profile_version="compact-fields.v1",
        universe_snapshot_id=universe_snapshot_id,
        feature_run_id=feature_run_id,
        as_of=as_of,
        known_at=NOW + timedelta(seconds=1),
        coverage=1,
        members=members,
    )


@pytest.mark.asyncio
async def test_candidate_comparison_atomic_publish_replay_and_immutability() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    code_start = 100_000 + uuid4().int % 800_000
    if code_start > 899_980:
        code_start = 899_980
    securities = tuple(
        SecurityMember(
            code=f"{code_start + index:06d}",
            market=Market.SH,
            name=f"comparison fixture {index + 1}",
        )
        for index in range(20)
    )
    snapshot = UniverseSnapshot.build(
        UniverseSnapshotContent(
            snapshot_id=uuid4(),
            source_code=f"comparison-pg-{uuid4().hex}",
            status=UniverseSnapshotStatus.PRIMARY,
            as_of=NOW - timedelta(minutes=1),
            fetch_time=NOW - timedelta(minutes=1),
            known_at=NOW - timedelta(minutes=1),
            coverage=1,
            stale=False,
            members=securities,
        )
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(snapshot)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        targets = await uow.universes.targets(snapshot.snapshot_id)
    security_ids = {target.code: target.security_id for target in targets}
    feature_run_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.feature_runs "
                "(feature_run_id, as_of, universe_snapshot_id, feature_version, status, "
                "expected_count, successful_count, failed_count, coverage, "
                "bar_revision_set_hash, input_manifest, error_summary, started_at, "
                "completed_at, content_hash) VALUES "
                "(:id, :as_of, :snapshot_id, 'comparison-fixture.v1', 'PUBLISHED', "
                "20, 20, 0, 1, :bar_hash, '{}'::jsonb, '{}'::jsonb, :as_of, :as_of, "
                ":content_hash)"
            ),
            {
                "id": feature_run_id,
                "as_of": NOW,
                "snapshot_id": snapshot.snapshot_id,
                "bar_hash": uuid4().hex * 2,
                "content_hash": uuid4().hex * 2,
            },
        )
    members = tuple(
        CandidateComparisonMember(
            security_id=security_ids[security.code],
            candidate_order=index,
            market=security.market.value,
            code=security.code,
            name=security.name,
            recall_summary={"channels": ["fixture"]},
            trend_summary={"return_20d": index / 100},
            quality={"status": "LIVE"},
            coverage=1,
            stale=False,
        )
        for index, security in enumerate(securities, start=1)
    )
    pack = _comparison_pack(
        universe_snapshot_id=snapshot.snapshot_id,
        feature_run_id=feature_run_id,
        members=members,
    )

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.candidate_comparisons.publish(pack)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert not await uow.candidate_comparisons.publish(pack)
        replay = await uow.candidate_comparisons.get(pack.comparison_pack_id)
        by_hash = await uow.candidate_comparisons.get_by_content_hash(pack.content_hash)
    assert replay == pack
    assert by_hash == pack
    assert [member.security_id for member in replay.members] == [
        member.security_id for member in members
    ]

    conflicting = _comparison_pack(
        universe_snapshot_id=snapshot.snapshot_id,
        feature_run_id=feature_run_id,
        members=members,
        comparison_pack_id=pack.comparison_pack_id,
        builder_version="comparison-persistence.v2",
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        with pytest.raises(RepositoryConflictError):
            await uow.candidate_comparisons.publish(conflicting)

    unavailable_members = list(members)
    unavailable_members[-1] = unavailable_members[-1].model_copy(
        update={"security_id": uuid4()}
    )
    unavailable = _comparison_pack(
        universe_snapshot_id=snapshot.snapshot_id,
        feature_run_id=feature_run_id,
        members=tuple(unavailable_members),
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        with pytest.raises(RepositoryNotFoundError):
            await uow.candidate_comparisons.publish(unavailable)

    future_input = _comparison_pack(
        universe_snapshot_id=snapshot.snapshot_id,
        feature_run_id=feature_run_id,
        members=members,
        as_of=NOW - timedelta(seconds=1),
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        with pytest.raises(ValueError, match="not available at comparison as_of"):
            await uow.candidate_comparisons.publish(future_input)

    async with engine.connect() as connection:
        count = await connection.scalar(
            text(
                "SELECT count(*) FROM v3.candidate_comparison_packs "
                "WHERE comparison_pack_id=:id"
            ),
            {"id": pack.comparison_pack_id},
        )
        member_count = await connection.scalar(
            text(
                "SELECT count(*) FROM v3.candidate_comparison_members "
                "WHERE comparison_pack_id=:id"
            ),
            {"id": pack.comparison_pack_id},
        )
        assert (count, member_count) == (1, 20)
        with pytest.raises(DBAPIError, match="immutable V3 record"):
            await connection.execute(
                text(
                    "UPDATE v3.candidate_comparison_packs SET coverage=0 "
                    "WHERE comparison_pack_id=:id"
                ),
                {"id": pack.comparison_pack_id},
            )
        await connection.rollback()
    await engine.dispose()
