"""RC-05A（CTX-002）：POSITION 主体 Context 源加载真实持仓事实（PostgreSQL）。"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.run_full_market_features import RunFullMarketFeaturesService
from app.v3.domain.context import ContextSubjectType
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
from app.v3.domain.portfolio import (
    AccountCreate,
    TradeConfirm,
    TradeDraftCreate,
    TradeSide,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.repositories.errors import RepositoryNotFoundError


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 8, 27, 8, tzinfo=timezone.utc)


def _make_revision(security_id) -> BarSeriesRevision:
    bars = tuple(
        MarketBar(
            bar_time=NOW - timedelta(days=60 - index),
            open=10.0, high=10.6 + index * 0.05, low=9.8, close=10.0 + index * 0.05,
            volume=1_000_000, amount=1e7,
            fetch_time=NOW - timedelta(minutes=1),
        )
        for index in range(60)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=security_id, period=BarPeriod.DAY,
        adjust_type=AdjustType.QFQ, source="fixture", upstream_source="fixture",
        raw_bar_available=False,
        point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="fixture QFQ only",
        known_at=NOW - timedelta(minutes=1), bars=bars,
    ))


async def test_position_context_source_loads_real_portfolio_projection() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    snapshot = UniverseSnapshot.build(UniverseSnapshotContent(
        snapshot_id=uuid4(), source_code=f"posctx-{uuid4().hex}",
        status=UniverseSnapshotStatus.PRIMARY, as_of=NOW - timedelta(minutes=3),
        fetch_time=NOW - timedelta(minutes=3), known_at=NOW - timedelta(minutes=3),
        coverage=1.0, stale=False,
        members=(SecurityMember(code="600300", market=Market.SH, name="pos ctx"),),
    ))
    index_revision = IndexBenchmarkRevision.build(IndexBenchmarkRevisionContent(
        revision_id=uuid4(), benchmark_code="HS300", source="fixture",
        upstream_source="fixture",
        fetch_time=NOW - timedelta(minutes=1), known_at=NOW - timedelta(minutes=1),
        bars=tuple(
            IndexBenchmarkBar(
                bar_time=NOW - timedelta(days=60 - index), close=3800 + index,
                amount=1e11,
            )
            for index in range(60)
        ),
    ))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(snapshot) is True
        assert await uow.index_benchmarks.publish(index_revision) is True
        await uow.commit()
        target = (await uow.universes.targets(snapshot.snapshot_id))[0]
        security_id = target.security_id
        assert await uow.bars.publish_series_revision(_make_revision(security_id)) is True
        await uow.commit()

    run = await RunFullMarketFeaturesService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW
    ).execute(universe_snapshot_id=snapshot.snapshot_id, as_of=NOW)

    account_id = uuid4()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_account(AccountCreate(account_id=account_id, name="pos ctx"))
        draft_id = await uow.portfolios.add_trade_draft(TradeDraftCreate(
            account_id=account_id, security_id=security_id, side=TradeSide.BUY,
            trade_time=NOW - timedelta(days=10), price=Decimal("10.0"),
            quantity=Decimal("1000"), fee=Decimal("5"),
        ))
        await uow.portfolios.confirm_trade(draft_id, TradeConfirm(
            idempotency_key=f"posctx-{uuid4().hex}", confirmed_by="fixture",
        ))
        await uow.commit()

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        source = await uow.context_packs.load_source(
            subject_type=ContextSubjectType.POSITION.value,
            subject_id=f"{account_id}:SH:600300",
            as_of=NOW,
            feature_run_id=run.feature_run_id,
        )
    assert source is not None
    assert source.code == "600300"
    assert source.feature is not None
    assert source.portfolio is not None
    assert source.portfolio.account_id == account_id
    assert source.portfolio.quantity == Decimal("1000")
    assert source.portfolio.cost_basis == Decimal("10005")
    assert source.portfolio.average_cost == Decimal("10.005")
    assert source.portfolio.realized_pnl == Decimal("0")
    assert source.portfolio.cost_method == "WEIGHTED_AVERAGE"

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        with pytest.raises(RepositoryNotFoundError):
            await uow.context_packs.load_source(
                subject_type=ContextSubjectType.POSITION.value,
                subject_id=f"{uuid4()}:SH:600300",
                as_of=NOW,
                feature_run_id=run.feature_run_id,
            )
    await engine.dispose()
