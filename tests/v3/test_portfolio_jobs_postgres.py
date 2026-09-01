from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.market_data import (
    CorporateAction,
    CorporateActionContent,
    CorporateActionType,
)
from app.v3.domain.portfolio import (
    AccountCreate,
    OpeningPositionCreate,
    PortfolioAdjustmentCreate,
)
from app.v3.domain.portfolio import (
    AdjustmentConfirmation,
    AdjustmentType,
)
from app.v3.infrastructure.db.models import (
    OperationalHealthEventModel,
    PortfolioAdjustmentModel,
    PositionProjectionModel,
    SecurityModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.application.match_corporate_actions import MatchCorporateActionsService
from app.v3.application.verify_position_projections import (
    VerifyPositionProjectionsService,
)


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)


async def _seed_account(sessions, with_opening: bool = True) -> tuple[UUID, UUID]:
    security_id = uuid4()
    account = AccountCreate(name=f"jobs-{uuid4().hex}")
    async with sessions() as session:
        session.add(SecurityModel(
            security_id=security_id,
            code=f"{security_id.int % 1_000_000:06d}",
            market="SH", name="portfolio jobs acceptance",
        ))
        await session.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_account(account)
        if with_opening:
            await uow.portfolios.add_opening_position(
                OpeningPositionCreate(
                    account_id=account.account_id, security_id=security_id,
                    baseline_time=NOW - __import__("datetime").timedelta(days=30),
                    quantity=Decimal("100"), average_cost=Decimal("10"),
                    source="ACCEPTANCE", confirmed_by="acceptance-human",
                )
            )
        await uow.commit()
    return account.account_id, security_id


async def _publish_action(
    sessions,
    security_id: UUID,
    *,
    action_type: CorporateActionType,
    payload: dict,
    effective_time: datetime | None = None,
) -> UUID:
    content = CorporateActionContent(
        corporate_action_id=uuid4(), security_id=security_id,
        action_type=action_type, effective_time=effective_time or NOW,
        payload=payload, source="TEST", source_reference=f"test-{uuid4().hex}",
        fetch_time=NOW - __import__("datetime").timedelta(hours=1), known_at=NOW,
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.corporate_actions.publish(CorporateAction.build(content))
        await uow.commit()
    return content.corporate_action_id


async def _adjustments(sessions, account_id: UUID, security_id: UUID):
    async with sessions() as session:
        return (
            await session.scalars(
                select(PortfolioAdjustmentModel).where(
                    PortfolioAdjustmentModel.account_id == account_id,
                    PortfolioAdjustmentModel.security_id == security_id,
                ).order_by(PortfolioAdjustmentModel.adjustment_sequence)
            )
        ).all()


@pytest.mark.asyncio
async def test_corporate_action_match_creates_pending_cash_draft() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account(sessions)
    action_id = await _publish_action(
        sessions, security_id, action_type=CorporateActionType.CASH_DIVIDEND,
        payload={"cash_per_share": "0.5"}, effective_time=NOW,
    )
    service = MatchCorporateActionsService(lambda: SQLAlchemyUnitOfWork(sessions))
    result = await service.execute(effective_from=NOW, effective_to=NOW)
    assert result.scanned == 1
    assert result.drafts_created == 1

    adjustments = await _adjustments(sessions, account_id, security_id)
    assert len(adjustments) == 1
    draft = adjustments[0]
    assert draft.corporate_action_id == action_id
    assert draft.confirmation_status == "PENDING_RECONCILIATION"
    assert draft.confirmed_by is None
    assert draft.cash_delta == Decimal("50")
    assert draft.quantity_delta == Decimal("0")
    assert draft.source == "CORPORATE_ACTION_MATCH"

    # PENDING 草稿不得进入 Projection；Opening 基准是起点，不产生现金流
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account_id, security_id)
    assert position is not None
    assert position["quantity"] == Decimal("100")
    assert position["cash_impact"] == Decimal("0")

    # 幂等：重复运行不产生重复草稿
    rerun = await service.execute(effective_from=NOW, effective_to=NOW)
    assert rerun.drafts_created == 0
    assert len(await _adjustments(sessions, account_id, security_id)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_corporate_action_match_creates_stock_quantity_draft() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account(sessions)
    await _publish_action(
        sessions, security_id, action_type=CorporateActionType.STOCK_DISTRIBUTION,
        payload={"stock_ratio": "0.3"}, effective_time=NOW + timedelta(hours=1),
    )
    service = MatchCorporateActionsService(lambda: SQLAlchemyUnitOfWork(sessions))
    result = await service.execute(
        effective_from=NOW + timedelta(hours=1), effective_to=NOW + timedelta(hours=1)
    )
    assert result.drafts_created == 1
    adjustments = await _adjustments(sessions, account_id, security_id)
    assert adjustments[0].quantity_delta == Decimal("30")
    assert adjustments[0].adjustment_type == "STOCK_DIVIDEND"
    assert adjustments[0].confirmation_status == "PENDING_RECONCILIATION"
    await engine.dispose()


@pytest.mark.asyncio
async def test_corporate_action_match_marks_unprovable_amounts_unknown() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account(sessions)
    await _publish_action(
        sessions, security_id, action_type=CorporateActionType.CASH_DIVIDEND,
        payload={}, effective_time=NOW + timedelta(hours=2),
    )
    service = MatchCorporateActionsService(lambda: SQLAlchemyUnitOfWork(sessions))
    result = await service.execute(
        effective_from=NOW + timedelta(hours=2), effective_to=NOW + timedelta(hours=2)
    )
    assert result.drafts_created == 1
    adjustments = await _adjustments(sessions, account_id, security_id)
    draft = adjustments[0]
    assert draft.source == "UNKNOWN"
    assert draft.cash_delta == Decimal("0")
    assert draft.quantity_delta == Decimal("0")
    assert draft.confirmation_status == "PENDING_RECONCILIATION"
    await engine.dispose()


@pytest.mark.asyncio
async def test_projection_verify_passes_on_consistent_projection() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account(sessions)
    service = VerifyPositionProjectionsService(lambda: SQLAlchemyUnitOfWork(sessions))
    report = await service.execute()
    assert report.checked >= 1
    matching = [
        item for item in report.mismatches
        if item["account_id"] == str(account_id)
    ]
    assert matching == []
    assert report.events_written == 0
    async with sessions() as session:
        events = (
            await session.scalars(select(OperationalHealthEventModel).where(
                OperationalHealthEventModel.component == "v3.portfolio"
            ))
        ).all()
    assert events == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_projection_verify_reports_drift_without_silent_fix() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account(sessions)
    async with sessions() as session:
        await session.execute(
            update(PositionProjectionModel)
            .where(
                PositionProjectionModel.account_id == account_id,
                PositionProjectionModel.security_id == security_id,
            )
            .values(quantity=PositionProjectionModel.quantity + 1)
        )
        await session.commit()
    service = VerifyPositionProjectionsService(lambda: SQLAlchemyUnitOfWork(sessions))
    report = await service.execute()
    matching = [
        item for item in report.mismatches
        if item["account_id"] == str(account_id)
    ]
    assert len(matching) == 1
    assert report.events_written == 1
    async with sessions() as session:
        events = (
            await session.scalars(select(OperationalHealthEventModel).where(
                OperationalHealthEventModel.component == "v3.portfolio",
                OperationalHealthEventModel.capability == "projection_verify",
                OperationalHealthEventModel.status == "FAILED",
            ))
        ).all()
    assert len(events) == 1
    assert events[0].metadata_payload["account_id"] == str(account_id)
    # 只报警，不静默修复
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account_id, security_id)
    assert position["quantity"] == Decimal("101")
    await engine.dispose()


@pytest.mark.asyncio
async def test_confirmed_adjustments_respect_opening_baseline_boundary() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account(sessions, with_opening=False)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_adjustment(PortfolioAdjustmentCreate(
            account_id=account_id, security_id=security_id,
            adjustment_type=AdjustmentType.CASH_DIVIDEND,
            effective_time=NOW - timedelta(days=40),
            quantity_delta=Decimal("10"), cash_delta=Decimal("0"),
            cost_basis_delta=Decimal("0"), source="ACCEPTANCE",
            source_reference="acc-pre-baseline",
            known_at=NOW - timedelta(days=40),
            confirmation_status=AdjustmentConfirmation.CONFIRMED,
            confirmed_by="acceptance-human",
        ))
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account_id, security_id)
    assert position is not None
    assert position["quantity"] == Decimal("10")

    # 基线晚于 Adjustment 生效时间：基准已包含该 Adjustment，不再叠加
    baseline = NOW - timedelta(days=30)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_opening_position(OpeningPositionCreate(
            account_id=account_id, security_id=security_id,
            baseline_time=baseline, quantity=Decimal("100"),
            average_cost=Decimal("10"), source="ACCEPTANCE",
            confirmed_by="acceptance-human",
        ))
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account_id, security_id)
    assert position is not None
    assert position["quantity"] == Decimal("100")

    # 基线之后的 Adjustment 正常叠加
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_adjustment(PortfolioAdjustmentCreate(
            account_id=account_id, security_id=security_id,
            adjustment_type=AdjustmentType.STOCK_DIVIDEND,
            effective_time=NOW - timedelta(days=10),
            quantity_delta=Decimal("5"), cash_delta=Decimal("0"),
            cost_basis_delta=Decimal("0"), source="ACCEPTANCE",
            source_reference="acc-post-baseline",
            known_at=NOW - timedelta(days=10),
            confirmation_status=AdjustmentConfirmation.CONFIRMED,
            confirmed_by="acceptance-human",
        ))
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account_id, security_id)
    await engine.dispose()
    assert position is not None
    assert position["quantity"] == Decimal("105")
