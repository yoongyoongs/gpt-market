from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.portfolio import (
    AccountCreate,
    OpeningPositionCreate,
    TradeConfirm,
    TradeDraftCreate,
    TradeSide,
)
from app.v3.infrastructure.db.models import (
    PositionProjectionModel,
    SecurityModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
BASELINE = datetime(2026, 8, 1, 8, tzinfo=timezone.utc)


async def _seed_account(sessions) -> tuple[AccountCreate, UUID]:
    security_id = uuid4()
    account = AccountCreate(name=f"opening-{uuid4().hex}")
    async with sessions() as session:
        session.add(
            SecurityModel(
                security_id=security_id,
                code=f"{security_id.int % 1_000_000:06d}",
                market="SH",
                name="opening baseline acceptance",
            )
        )
        await session.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_account(account)
        await uow.commit()
    return account, security_id


async def _confirm_trade(
    sessions,
    account_id: UUID,
    security_id: UUID,
    *,
    side: TradeSide,
    trade_time: datetime,
    price: str,
    quantity: str,
) -> UUID:
    draft = TradeDraftCreate(
        account_id=account_id,
        security_id=security_id,
        side=side,
        trade_time=trade_time,
        price=Decimal(price),
        quantity=Decimal(quantity),
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_trade_draft(draft)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        trade_id = await uow.portfolios.confirm_trade(
            draft.draft_id,
            TradeConfirm(
                idempotency_key=f"opening-baseline-{uuid4().hex}",
                confirmed_by="acceptance-human",
            ),
        )
        await uow.commit()
    return trade_id


async def _add_opening(
    sessions,
    account_id: UUID,
    security_id: UUID,
    *,
    baseline_time: datetime,
    quantity: str,
    average_cost: str,
) -> UUID:
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        opening_id = await uow.portfolios.add_opening_position(
            OpeningPositionCreate(
                account_id=account_id,
                security_id=security_id,
                baseline_time=baseline_time,
                quantity=Decimal(quantity),
                average_cost=Decimal(average_cost),
                source="ACCEPTANCE",
                confirmed_by="acceptance-human",
            )
        )
        await uow.commit()
    return opening_id


async def _position(sessions, account_id: UUID, security_id: UUID) -> dict:
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        return await uow.portfolios.position(account_id, security_id)


@pytest.mark.asyncio
async def test_opening_baseline_excludes_pre_baseline_trades() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account, security_id = await _seed_account(sessions)
    await _confirm_trade(
        sessions, account.account_id, security_id,
        side=TradeSide.BUY,
        trade_time=datetime(2026, 7, 15, 6, tzinfo=timezone.utc),
        price="10", quantity="100",
    )
    await _add_opening(
        sessions, account.account_id, security_id,
        baseline_time=BASELINE, quantity="100", average_cost="10",
    )
    position = await _position(sessions, account.account_id, security_id)
    await engine.dispose()
    assert position is not None
    assert position["quantity"] == Decimal("100")
    assert position["cost_basis"] == Decimal("1000")


@pytest.mark.asyncio
async def test_trade_at_baseline_time_belongs_to_baseline() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account, security_id = await _seed_account(sessions)
    await _confirm_trade(
        sessions, account.account_id, security_id,
        side=TradeSide.BUY,
        trade_time=BASELINE,
        price="10", quantity="100",
    )
    await _add_opening(
        sessions, account.account_id, security_id,
        baseline_time=BASELINE, quantity="100", average_cost="10",
    )
    position = await _position(sessions, account.account_id, security_id)
    await engine.dispose()
    assert position is not None
    assert position["quantity"] == Decimal("100")


@pytest.mark.asyncio
async def test_post_baseline_trades_replay_on_top_of_baseline() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account, security_id = await _seed_account(sessions)
    await _add_opening(
        sessions, account.account_id, security_id,
        baseline_time=BASELINE, quantity="100", average_cost="10",
    )
    await _confirm_trade(
        sessions, account.account_id, security_id,
        side=TradeSide.BUY,
        trade_time=datetime(2026, 8, 5, 6, tzinfo=timezone.utc),
        price="12", quantity="50",
    )
    await _confirm_trade(
        sessions, account.account_id, security_id,
        side=TradeSide.SELL,
        trade_time=datetime(2026, 8, 6, 6, tzinfo=timezone.utc),
        price="15", quantity="30",
    )
    position = await _position(sessions, account.account_id, security_id)
    await engine.dispose()
    assert position is not None
    # 100@10 baseline + 50@12 buy - 30 sell (avg (1000+600)/150 = 10.666667)
    assert position["quantity"] == Decimal("120")
    assert position["realized_pnl"] == Decimal("130")


@pytest.mark.asyncio
async def test_latest_opening_supersedes_older_baseline_and_rebuild_is_stable() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account, security_id = await _seed_account(sessions)
    await _confirm_trade(
        sessions, account.account_id, security_id,
        side=TradeSide.BUY,
        trade_time=datetime(2026, 7, 15, 6, tzinfo=timezone.utc),
        price="10", quantity="100",
    )
    await _add_opening(
        sessions, account.account_id, security_id,
        baseline_time=datetime(2026, 7, 20, 8, tzinfo=timezone.utc),
        quantity="200", average_cost="9",
    )
    await _add_opening(
        sessions, account.account_id, security_id,
        baseline_time=BASELINE, quantity="100", average_cost="10",
    )
    position = await _position(sessions, account.account_id, security_id)
    assert position is not None
    assert position["quantity"] == Decimal("100")
    assert position["cost_basis"] == Decimal("1000")

    original_input_hash = position["input_hash"]
    async with sessions() as session:
        await session.execute(
            delete(PositionProjectionModel).where(
                PositionProjectionModel.account_id == account.account_id,
                PositionProjectionModel.security_id == security_id,
            )
        )
        await session.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        rebuilt = await uow.portfolios.rebuild_position(
            account.account_id, security_id
        )
        await uow.commit()
    await engine.dispose()
    assert rebuilt["quantity"] == Decimal("100")
    assert rebuilt["cost_basis"] == Decimal("1000")
    assert rebuilt["input_hash"] == original_input_hash
