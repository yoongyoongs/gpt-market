from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.run_full_market_features import RunFullMarketFeaturesService
from app.v3.application.run_multi_recall import RunMultiRecallService
from app.v3.domain.market_data import (
    Market,
    SecurityMember,
    UniverseSnapshot,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)
from app.v3.domain.recall import RecallChannel
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.providers.calendar import TradingCalendarMetadata
from app.v3.providers.recall import ChannelEvaluation, RecallCandidate
from tests.v3.test_phase3_feature_postgres import make_revision


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured")
NOW = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)


class Calendar:
    metadata = TradingCalendarMetadata(
        source="fixture", source_version="v1", calendar_code="XSHG",
        coverage_start=date(2020, 1, 1), coverage_end=date(2030, 1, 1),
    )

    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5


class AllRowsChannel:
    channel = RecallChannel.build(
        code="POSTGRES_ALL_ROWS", version="v1", configuration={"fixture": True},
        description="真实数据库原子发布验收通道",
    )

    def evaluate(self, features, _evidence):
        return ChannelEvaluation(
            evaluated_count=len(features), unavailable_count=0,
            candidates=tuple(RecallCandidate(
                security_id=item.security_id, strength=0.5,
                reasons=("fixture=true",), matched_features={"fixture": True},
                coverage=item.coverage,
            ) for item in features),
        )


@pytest.mark.asyncio
async def test_recall_run_atomic_publish_replay_full_observations_and_immutability() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    snapshot = UniverseSnapshot.build(UniverseSnapshotContent(
        snapshot_id=uuid4(), source_code=f"recall-pg-{uuid4().hex}",
        status=UniverseSnapshotStatus.PRIMARY, as_of=NOW - timedelta(minutes=2),
        fetch_time=NOW - timedelta(minutes=2), known_at=NOW - timedelta(minutes=2),
        coverage=1, stale=False,
        members=(
            SecurityMember(code="600011", market=Market.SH, name="recall one"),
            SecurityMember(code="000012", market=Market.SZ, name="recall two"),
        ),
    ))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(snapshot)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        targets = await uow.universes.targets(snapshot.snapshot_id)
        for seed, target in enumerate(targets):
            assert await uow.bars.publish_series_revision(make_revision(target.security_id, seed))
        await uow.commit()
    feature_run = await RunFullMarketFeaturesService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW
    ).execute(universe_snapshot_id=snapshot.snapshot_id, as_of=NOW)
    service = RunMultiRecallService(
        lambda: SQLAlchemyUnitOfWork(sessions), Calendar(),
        channels=(AllRowsChannel(),), clock=lambda: NOW + timedelta(minutes=1),
    )
    first = await service.execute(feature_run_id=feature_run.feature_run_id)
    replay = await service.execute(feature_run_id=feature_run.feature_run_id)
    assert replay.recall_run_id == first.recall_run_id
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        result_first = await uow.recalls.read_results(
            recall_run_id=first.recall_run_id,
            channel_code=AllRowsChannel.channel.code,
            limit=1,
            cursor=None,
        )
        assert result_first is not None and result_first.next_cursor is not None
        result_second = await uow.recalls.read_results(
            recall_run_id=first.recall_run_id,
            channel_code=AllRowsChannel.channel.code,
            limit=1,
            cursor=result_first.next_cursor,
        )
        raw_page = await uow.recalls.read_raw(
            recall_run_id=first.recall_run_id, limit=2, cursor=None,
        )
    assert result_second is not None
    assert result_first.items[0].security_id != result_second.items[0].security_id
    assert raw_page is not None and len(raw_page.items) == 2
    assert all("score" not in item.model_dump_json().lower() for item in raw_page.items)
    async with engine.connect() as connection:
        result_count = await connection.scalar(text(
            "SELECT count(*) FROM v3.recall_results WHERE recall_run_id=:id"
        ), {"id": first.recall_run_id})
        raw_count = await connection.scalar(text(
            "SELECT count(*) FROM v3.raw_opportunities WHERE recall_run_id=:id"
        ), {"id": first.recall_run_id})
        observation_count = await connection.scalar(text(
            "SELECT count(*) FROM v3.performance_observations WHERE recall_run_id=:id"
        ), {"id": first.recall_run_id})
        pending_future_count = await connection.scalar(text(
            "SELECT count(*) FROM v3.performance_observations "
            "WHERE recall_run_id=:id AND status='PENDING' "
            "AND (future_price IS NOT NULL OR raw_return IS NOT NULL)"
        ), {"id": first.recall_run_id})
        assert (result_count, raw_count, observation_count, pending_future_count) == (2, 2, 6, 0)
        with pytest.raises(DBAPIError, match="immutable V3 record"):
            await connection.execute(text(
                "UPDATE v3.recall_runs SET coverage=0 WHERE recall_run_id=:id"
            ), {"id": first.recall_run_id})
        await connection.rollback()
    await engine.dispose()
