from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.portfolio import (
    AccountCreate,
    TradeConfirm,
    TradeCorrectionCreate,
    TradeDraftCreate,
    TradeSide,
)
from app.v3.infrastructure.db.models import (
    PositionProjectionModel,
    SecurityModel,
    TradeCorrectionModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.repositories.errors import RepositoryConflictError


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)


async def _seed_buy_trade(sessions) -> tuple[AccountCreate, UUID, UUID]:
    security_id = uuid4()
    account = AccountCreate(name=f"correction-{uuid4().hex}")
    async with sessions() as session:
        session.add(
            SecurityModel(
                security_id=security_id,
                code=f"{security_id.int % 1_000_000:06d}",
                market="SH",
                name="correction acceptance",
            )
        )
        await session.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_account(account)
        await uow.commit()
    draft = TradeDraftCreate(
        account_id=account.account_id,
        security_id=security_id,
        side=TradeSide.BUY,
        trade_time=NOW,
        price=Decimal("10"),
        quantity=Decimal("100"),
        fee=Decimal("5"),
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_trade_draft(draft)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        trade_id = await uow.portfolios.confirm_trade(
            draft.draft_id,
            TradeConfirm(
                idempotency_key=f"correction-seed-{uuid4().hex}",
                confirmed_by="acceptance-human",
            ),
        )
        await uow.commit()
    return account, security_id, trade_id


@pytest.mark.asyncio
async def test_trade_correction_chain_is_serial_and_rebuildable() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account, security_id, trade_id = await _seed_buy_trade(sessions)
    commands = (
        TradeCorrectionCreate(
            trade_id=trade_id,
            correction_type="CORRECT",
            replacement={"quantity": "80"},
            reason="correct quantity",
            confirmed_by="acceptance-human",
        ),
        TradeCorrectionCreate(
            trade_id=trade_id,
            correction_type="CORRECT",
            replacement={"fee": "4"},
            reason="correct fee",
            confirmed_by="acceptance-human",
        ),
    )

    async def append(command: TradeCorrectionCreate) -> None:
        async with SQLAlchemyUnitOfWork(sessions) as uow:
            await uow.portfolios.add_trade_correction(command)
            await uow.commit()

    await asyncio.gather(*(append(command) for command in commands))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account.account_id, security_id)
    assert position is not None
    assert position["quantity"] == Decimal("80")
    assert position["cost_basis"] == Decimal("804")
    assert position["average_cost"] == Decimal("10.05")

    async with sessions() as session:
        corrections = (
            await session.scalars(
                select(TradeCorrectionModel)
                .where(TradeCorrectionModel.trade_id == trade_id)
                .order_by(TradeCorrectionModel.correction_sequence)
            )
        ).all()
    assert len(corrections) == 2
    assert corrections[0].correction_sequence < corrections[1].correction_sequence
    assert (
        corrections[1].previous_effective_hash == corrections[0].effective_hash
    )

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
    assert rebuilt["quantity"] == Decimal("80")
    assert rebuilt["cost_basis"] == Decimal("804")
    assert rebuilt["input_hash"] == original_input_hash


@pytest.mark.asyncio
async def test_trade_reverse_is_terminal_in_postgres() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account, security_id, trade_id = await _seed_buy_trade(sessions)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_trade_correction(
            TradeCorrectionCreate(
                trade_id=trade_id,
                correction_type="REVERSE",
                reason="reverse mistaken trade",
                confirmed_by="acceptance-human",
            )
        )
        await uow.commit()
    with pytest.raises(RepositoryConflictError, match="reversed trade is terminal"):
        async with SQLAlchemyUnitOfWork(sessions) as uow:
            await uow.portfolios.add_trade_correction(
                TradeCorrectionCreate(
                    trade_id=trade_id,
                    correction_type="CORRECT",
                    replacement={"fee": "4"},
                    reason="must not resurrect",
                    confirmed_by="acceptance-human",
                )
            )
            await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account.account_id, security_id)
    await engine.dispose()
    assert position is not None
    assert position["quantity"] == Decimal("0")
