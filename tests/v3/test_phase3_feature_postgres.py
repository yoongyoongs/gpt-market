from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.run_full_market_features import RunFullMarketFeaturesService
from app.v3.domain.features import FeatureQuery, FeatureSortField
from app.v3.domain.market_data import (
    AdjustType, BarPeriod, BarSeriesRevision, BarSeriesRevisionContent, Market,
    MarketBar, PointInTimePrecision, SecurityMember, UniverseSnapshot,
    UniverseSnapshotContent, UniverseSnapshotStatus,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured")
NOW = datetime(2026, 8, 30, 8, tzinfo=timezone.utc)


def make_revision(security_id, seed: int) -> BarSeriesRevision:
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
        adjust_type=AdjustType.QFQ, source="phase3-fixture", upstream_source="fixture",
        raw_bar_available=False, point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="fixture QFQ only", known_at=NOW - timedelta(seconds=1), bars=bars,
    ))


@pytest.mark.asyncio
async def test_feature_run_publish_query_cursor_regime_and_immutability() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    snapshot = UniverseSnapshot.build(UniverseSnapshotContent(
        snapshot_id=uuid4(), source_code=f"phase3-{uuid4().hex}",
        status=UniverseSnapshotStatus.PRIMARY, as_of=NOW - timedelta(minutes=2),
        fetch_time=NOW - timedelta(minutes=2), known_at=NOW - timedelta(minutes=2),
        coverage=1.0, stale=False,
        members=(
            SecurityMember(code="600001", market=Market.SH, name="fixture one"),
            SecurityMember(code="000002", market=Market.SZ, name="fixture two"),
        ),
    ))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(snapshot) is True
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        targets = await uow.universes.targets(snapshot.snapshot_id)
        for seed, target in enumerate(targets):
            assert await uow.bars.publish_series_revision(make_revision(target.security_id, seed)) is True
        await uow.commit()

    service = RunFullMarketFeaturesService(lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW)
    run = await service.execute(universe_snapshot_id=snapshot.snapshot_id, as_of=NOW)
    replay = await service.execute(universe_snapshot_id=snapshot.snapshot_id, as_of=NOW)
    assert run.feature_run_id == replay.feature_run_id
    assert run.successful_count == 2
    assert run.failed_count == 0

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.features.query(FeatureQuery(
            feature_run_id=run.feature_run_id, sort_by=FeatureSortField.RETURN_20D,
            descending=True, fields=("code", "return_20d"), limit=1,
        ))
        assert first is not None and first.next_cursor is not None
        second = await uow.features.query(FeatureQuery(
            feature_run_id=run.feature_run_id, sort_by=FeatureSortField.RETURN_20D,
            descending=True, fields=("code", "return_20d"), limit=1,
            cursor=first.next_cursor,
        ))
        regime = await uow.features.latest_regime()
    assert second is not None
    assert first.items[0]["code"] != second.items[0]["code"]
    assert regime is not None and regime.feature_run_id == run.feature_run_id
    assert regime.limit_structure["status"] == "UNKNOWN"

    async with engine.connect() as connection:
        with pytest.raises(DBAPIError, match="immutable V3 record"):
            await connection.execute(text(
                "UPDATE v3.security_features SET stale=true WHERE feature_run_id=:run_id"
            ), {"run_id": run.feature_run_id})
        await connection.rollback()
    await engine.dispose()
