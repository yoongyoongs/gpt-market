"""RC-08D Idempotency Matrix 真实 PostgreSQL 集成测试。

整改方案 §11.5：每个 WRITE 必须有稳定幂等策略，对外契约可重复请求。
重复同一业务请求 → 返回同一实体 id、不产生第二行；
同业务身份但内容不同 → RepositoryConflictError（409）。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.portfolio import (
    OpeningPositionCreate,
    PortfolioAdjustmentCreate,
    ReconciliationCreate,
    TradeConfirm,
    TradeCorrectionCreate,
)
from app.v3.domain.strategy import ExperimentEventCommand, StrategyVersionCreate
from app.v3.domain.portfolio import AdjustmentConfirmation
from app.v3.domain.strategy import ActorType
from app.v3.infrastructure.db.models import (
    AccountModel,
    OpeningPositionModel,
    PortfolioAdjustmentModel,
    ReconciliationModel,
    SecurityModel,
    StrategyExperimentEventModel,
    StrategyVersionModel,
    TradeCorrectionModel,
    TradeDraftModel,
    TradeLedgerModel,
    OperationalHealthEventModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.repositories.errors import RepositoryConflictError
from tests.v3.test_shadow_executor_postgres import _seed_experiment

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)


async def _seed_account_security(sessions):
    account_id, security_id = uuid4(), uuid4()
    async with sessions() as session:
        session.add(AccountModel(
            account_id=account_id, name=f"idem-{uuid4().hex[:8]}",
            currency="CNY", cost_method="MOVING_AVERAGE",
        ))
        session.add(SecurityModel(
            security_id=security_id, market="SH", code=uuid4().hex[:6],
            name="idem-seed",
        ))
        await session.commit()
    return account_id, security_id


def _opening(account_id, security_id, **overrides):
    values = dict(
        baseline_time=NOW, quantity=Decimal("100"),
        average_cost=Decimal("10.5"), source="MANUAL",
        confirmed_by="op-1",
    )
    values.update(overrides)
    return OpeningPositionCreate(
        account_id=account_id, security_id=security_id, **values,
    )


async def _count(model, column, value, sessions):
    async with sessions() as session:
        rows = (await session.scalars(
            select(model).where(column == value)
        )).all()
    return rows


async def test_opening_repeat_returns_same_id_and_single_row() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.portfolios.add_opening_position(
            _opening(account_id, security_id)
        )
        await uow.commit()
    # 重复请求：全新 command 对象（新 PK），同一业务内容
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        second = await uow.portfolios.add_opening_position(
            _opening(account_id, security_id)
        )
        await uow.commit()
    assert second == first
    rows = await _count(
        OpeningPositionModel, OpeningPositionModel.account_id, account_id, sessions
    )
    assert len(rows) == 1
    await engine.dispose()


async def test_opening_same_identity_different_content_conflicts() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_opening_position(_opening(account_id, security_id))
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        try:
            await uow.portfolios.add_opening_position(_opening(
                account_id, security_id, quantity=Decimal("200"),
            ))
        except RepositoryConflictError:
            await uow.rollback()
            await engine.dispose()
            return
        await uow.rollback()
    raise AssertionError("expected RepositoryConflictError for conflicting opening")


async def test_adjustment_repeat_returns_same_id_and_single_row() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)

    def _adjustment():
        return PortfolioAdjustmentCreate(
            account_id=account_id, security_id=security_id,
            adjustment_type="OTHER", effective_time=NOW,
            quantity_delta=Decimal("10"), known_at=NOW,
            confirmation_status=AdjustmentConfirmation.PENDING_RECONCILIATION,
        )

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.portfolios.add_adjustment(_adjustment())
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        second = await uow.portfolios.add_adjustment(_adjustment())
        await uow.commit()
    assert second == first
    rows = await _count(
        PortfolioAdjustmentModel,
        PortfolioAdjustmentModel.account_id, account_id, sessions,
    )
    assert len(rows) == 1
    await engine.dispose()


async def _seed_trade(sessions, account_id, security_id):
    trade_id, draft_id = uuid4(), uuid4()
    async with sessions() as session:
        session.add(TradeDraftModel(
            draft_id=draft_id, account_id=account_id, security_id=security_id,
            status="DRAFT",
            payload={"side": "BUY", "price": "10.5", "quantity": "100",
                     "fee": "5", "trade_time": NOW.isoformat(),
                     "source": "MANUAL"},
            field_confidence={},
        ))
        await session.flush()
        session.add(TradeLedgerModel(
            trade_id=trade_id, account_id=account_id,
            security_id=security_id, side="BUY", trade_time=NOW,
            price=Decimal("10.5"), quantity=Decimal("100"), fee=Decimal("5"),
            source="MANUAL", execution_deviation={},
            idempotency_key=f"seed-{uuid4().hex}", confirmed_by="op-1",
            content_hash=f"seed-{uuid4().hex}",
        ))
        await session.commit()
    return draft_id, trade_id


async def test_trade_confirm_repeat_returns_same_trade_id() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)
    draft_id, _ = await _seed_trade(sessions, account_id, security_id)
    key = f"confirm-{uuid4().hex}"
    command = TradeConfirm(idempotency_key=key, confirmed_by="op-1")
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.portfolios.confirm_trade(draft_id, command)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        second = await uow.portfolios.confirm_trade(draft_id, command)
        await uow.commit()
    assert second == first
    await engine.dispose()


async def test_correction_repeat_returns_same_id_and_single_row() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)
    _, trade_id = await _seed_trade(sessions, account_id, security_id)

    def _correction():
        return TradeCorrectionCreate(
            trade_id=trade_id, correction_type="REVERSE",
            reason="duplicate entry", confirmed_by="op-1",
        )

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.portfolios.add_trade_correction(_correction())
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        second = await uow.portfolios.add_trade_correction(_correction())
        await uow.commit()
    assert second == first
    rows = await _count(
        TradeCorrectionModel, TradeCorrectionModel.trade_id, trade_id, sessions
    )
    assert len(rows) == 1
    await engine.dispose()


async def test_reconciliation_repeat_returns_same_id_and_single_row() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)

    def _reconciliation():
        return ReconciliationCreate(
            account_id=account_id, security_id=security_id,
            reconciled_at=NOW,
            broker_facts={"quantity": "100"},
            projected_facts={"quantity": "100"},
            difference={}, reason="routine", resolution="MATCHED",
            confirmed_by="op-1",
        )

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.portfolios.add_reconciliation(_reconciliation())
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        second = await uow.portfolios.add_reconciliation(_reconciliation())
        await uow.commit()
    assert second == first
    rows = await _count(
        ReconciliationModel, ReconciliationModel.account_id, account_id, sessions
    )
    assert len(rows) == 1
    await engine.dispose()


async def test_strategy_version_repeat_returns_same_id_and_single_row() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    code = f"idem-{uuid4().hex[:8]}"

    def _version():
        return StrategyVersionCreate(
            strategy_code=code, version=1,
            configuration={"k": 1}, rationale="idem", created_by="op-1",
        )

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.strategies.add_strategy_version(_version())
        await uow._session.flush()
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        second = await uow.strategies.add_strategy_version(_version())
        await uow._session.flush()
        await uow.commit()
    assert second == first
    rows = await _count(
        StrategyVersionModel, StrategyVersionModel.strategy_code, code, sessions
    )
    assert len(rows) == 1
    await engine.dispose()


async def test_strategy_version_same_identity_different_content_conflicts() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    code = f"idem-{uuid4().hex[:8]}"
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.strategies.add_strategy_version(StrategyVersionCreate(
            strategy_code=code, version=1,
            configuration={"k": 1}, rationale="idem", created_by="op-1",
        ))
        await uow._session.flush()
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        try:
            await uow.strategies.add_strategy_version(StrategyVersionCreate(
                strategy_code=code, version=1,
                configuration={"k": 2}, rationale="idem", created_by="op-1",
            ))
            await uow._session.flush()
        except RepositoryConflictError:
            await uow.rollback()
            await engine.dispose()
            return
        await uow.rollback()
    raise AssertionError("expected RepositoryConflictError for conflicting version")


async def test_experiment_event_repeat_returns_same_event_id() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    experiment_id, _, _ = await _seed_experiment(sessions)
    command = ExperimentEventCommand(
        event_type="PAUSED", actor_type=ActorType.HUMAN,
        actor_id="op-1", reason="manual pause",
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.strategies.append_experiment_event(experiment_id, command)
        await uow._session.flush()
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        second = await uow.strategies.append_experiment_event(experiment_id, command)
        await uow._session.flush()
        await uow.commit()
    assert second == first
    async with sessions() as session:
        rows = (await session.scalars(select(StrategyExperimentEventModel).where(
            StrategyExperimentEventModel.experiment_id == experiment_id,
            StrategyExperimentEventModel.event_type == "PAUSED",
        ))).all()
    assert len(rows) == 1
    await engine.dispose()


async def test_health_event_repeat_returns_same_id_and_single_row() -> None:
    from app.v3.domain.strategy import OperationalHealthEventCreate

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    component = f"comp-{uuid4().hex[:8]}"

    def _event():
        return OperationalHealthEventCreate(
            component=component, capability="eastmoney-bars",
            status="HEALTHY", latency_ms=120.0,
            circuit_state="CLOSED", observed_at=NOW,
        )

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.strategies.add_health_event(_event())
        await uow._session.flush()
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        second = await uow.strategies.add_health_event(_event())
        await uow._session.flush()
        await uow.commit()
    assert second == first
    async with sessions() as session:
        rows = (await session.scalars(select(OperationalHealthEventModel).where(
            OperationalHealthEventModel.component == component,
        ))).all()
    assert len(rows) == 1
    await engine.dispose()
