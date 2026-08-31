from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.domain.hashing import canonical_hash
from app.v3.domain.portfolio import (
    AccountCreate,
    OpeningPositionCreate,
    PortfolioAdjustmentCreate,
    PortfolioPreferenceCreate,
    PositionSnapshotDraftCreate,
    ReconciliationCreate,
    TradeConfirm,
    TradeCorrectionCreate,
    TradeDraftCreate,
)
from app.v3.infrastructure.db.models import (
    AccountModel,
    EntryPlanModel,
    ImageImportModel,
    OpeningPositionModel,
    PortfolioAdjustmentModel,
    PositionProjectionModel,
    PositionSnapshotDraftModel,
    PortfolioPreferenceModel,
    ReconciliationModel,
    SecurityModel,
    TradeDraftModel,
    TradeCorrectionModel,
    TradeLedgerModel,
)
from app.v3.repositories.errors import RepositoryConflictError, RepositoryNotFoundError


class SQLAlchemyPortfolioRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_account(self, command: AccountCreate) -> UUID:
        self._session.add(AccountModel(
            account_id=command.account_id, name=command.name,
            currency=command.currency, cost_method=command.cost_method,
            status="ACTIVE",
        ))
        return command.account_id

    async def add_image_import(
        self, image_hash: str, image_reference: str, import_type: str,
        ocr_payload: dict[str, Any], field_regions: dict[str, Any],
    ) -> UUID:
        existing = await self._session.scalar(select(ImageImportModel).where(
            ImageImportModel.image_hash == image_hash
        ))
        if existing is not None:
            return existing.image_import_id
        image_import_id = uuid4()
        self._session.add(ImageImportModel(
            image_import_id=image_import_id, image_hash=image_hash,
            image_reference=image_reference, import_type=import_type,
            ocr_payload=ocr_payload, field_regions=field_regions,
        ))
        return image_import_id

    async def add_trade_draft(self, draft: TradeDraftCreate) -> UUID:
        if await self._session.get(AccountModel, draft.account_id) is None:
            raise RepositoryNotFoundError("account not found")
        if await self._session.get(SecurityModel, draft.security_id) is None:
            raise RepositoryNotFoundError("security not found")
        self._session.add(TradeDraftModel(
            draft_id=draft.draft_id, account_id=draft.account_id,
            security_id=draft.security_id, image_import_id=draft.image_import_id,
            status="DRAFT", payload=draft.model_dump(mode="json"),
            field_confidence=draft.field_confidence,
        ))
        return draft.draft_id

    async def add_position_draft(self, draft: PositionSnapshotDraftCreate) -> UUID:
        self._session.add(PositionSnapshotDraftModel(
            draft_id=draft.draft_id, account_id=draft.account_id,
            security_id=draft.security_id, image_import_id=draft.image_import_id,
            status="DRAFT", payload=draft.model_dump(mode="json"),
            field_confidence=draft.field_confidence,
        ))
        return draft.draft_id

    async def confirm_position_draft(
        self, draft_id: UUID, confirmed_by: str,
    ) -> UUID:
        draft = await self._session.get(PositionSnapshotDraftModel, draft_id, with_for_update=True)
        if draft is None:
            raise RepositoryNotFoundError("position snapshot draft not found")
        if draft.status != "DRAFT":
            raise RepositoryConflictError("position snapshot draft is no longer confirmable")
        payload = draft.payload
        opening = OpeningPositionCreate(
            account_id=draft.account_id, security_id=draft.security_id,
            baseline_time=datetime.fromisoformat(payload["as_of"]),
            quantity=Decimal(str(payload["quantity"])),
            average_cost=Decimal(str(payload["average_cost"])),
            source="CONFIRMED_POSITION_SCREENSHOT", confirmed_by=confirmed_by,
        )
        opening_id = await self.add_opening_position(opening)
        draft.status = "CONFIRMED"
        draft.confirmed_opening_position_id = opening_id
        draft.confirmed_by = confirmed_by
        draft.confirmed_at = datetime.now(timezone.utc)
        return opening_id

    async def confirm_trade(self, draft_id: UUID, command: TradeConfirm) -> UUID:
        existing = await self._session.scalar(select(TradeLedgerModel).where(
            TradeLedgerModel.idempotency_key == command.idempotency_key
        ))
        if existing is not None:
            return existing.trade_id
        draft = await self._session.get(TradeDraftModel, draft_id, with_for_update=True)
        if draft is None:
            raise RepositoryNotFoundError("trade draft not found")
        if draft.status != "DRAFT":
            raise RepositoryConflictError("trade draft is no longer confirmable")
        payload = draft.payload
        side = str(payload["side"])
        price = Decimal(str(payload["price"]))
        quantity = Decimal(str(payload["quantity"]))
        fee = Decimal(str(payload.get("fee", 0)))
        projection = await self._session.get(
            PositionProjectionModel, (draft.account_id, draft.security_id),
            with_for_update=True,
        )
        available = projection.quantity if projection is not None else Decimal("0")
        if side == "SELL" and quantity > available:
            raise RepositoryConflictError(
                f"oversell rejected: requested {quantity}, available {available}"
            )
        entry_plan_id = UUID(payload["entry_plan_id"]) if payload.get("entry_plan_id") else None
        entry_plan_version = payload.get("entry_plan_version")
        deviation: dict[str, Any]
        if entry_plan_id is None:
            deviation = {"mode": "MANUAL_TRADE_WITHOUT_AI_ENTRY"}
        else:
            plan = await self._session.get(EntryPlanModel, entry_plan_id)
            if plan is None or plan.version != entry_plan_version:
                raise RepositoryConflictError("bound entry plan version does not exist")
            deviation = self._execution_deviation(
                plan.plan, price, quantity, datetime.fromisoformat(payload["trade_time"])
            )
        trade_id = uuid4()
        content = {
            "draft_id": str(draft_id), "account_id": str(draft.account_id),
            "security_id": str(draft.security_id), "side": side,
            "trade_time": payload["trade_time"], "price": str(price),
            "quantity": str(quantity), "fee": str(fee),
            "entry_plan_id": str(entry_plan_id) if entry_plan_id else None,
            "entry_plan_version": entry_plan_version,
            "execution_deviation": deviation,
        }
        ledger = TradeLedgerModel(
            trade_id=trade_id, draft_id=draft_id, account_id=draft.account_id,
            security_id=draft.security_id, side=side,
            trade_time=datetime.fromisoformat(payload["trade_time"]),
            price=price, quantity=quantity, fee=fee,
            source=payload.get("source", "MANUAL"),
            source_reference=payload.get("source_reference"),
            decision_id=UUID(payload["decision_id"]) if payload.get("decision_id") else None,
            entry_plan_id=entry_plan_id, entry_plan_version=entry_plan_version,
            execution_deviation=deviation, idempotency_key=command.idempotency_key,
            confirmed_by=command.confirmed_by, content_hash=canonical_hash(content),
        )
        self._session.add(ledger)
        draft.status = "CONFIRMED"
        draft.idempotency_key = command.idempotency_key
        draft.confirmed_by = command.confirmed_by
        draft.confirmed_at = datetime.now(timezone.utc)
        await self._session.flush()
        await self.rebuild_position(draft.account_id, draft.security_id)
        return trade_id

    @staticmethod
    def _execution_deviation(
        plan: dict[str, Any], price: Decimal, quantity: Decimal, trade_time: datetime,
    ) -> dict[str, Any]:
        low = Decimal(str(plan["entry_price_low"])) if plan.get("entry_price_low") is not None else None
        high = Decimal(str(plan["entry_price_high"])) if plan.get("entry_price_high") is not None else None
        price_delta = Decimal("0")
        if low is not None and price < low:
            price_delta = price - low
        elif high is not None and price > high:
            price_delta = price - high
        planned_quantity = Decimal(str(plan["quantity"])) if plan.get("quantity") is not None else None
        return {
            "price_delta_to_entry_window": str(price_delta),
            "quantity_delta": str(quantity - planned_quantity) if planned_quantity is not None else "UNKNOWN",
            "trade_time": trade_time.isoformat(),
            "trigger_match": "UNKNOWN",
            "cancel_condition_violated": "UNKNOWN",
            "plan_snapshot_hash": canonical_hash(plan),
        }

    async def add_opening_position(self, opening: OpeningPositionCreate) -> UUID:
        content = opening.model_dump(mode="json")
        self._session.add(OpeningPositionModel(
            opening_position_id=opening.opening_position_id,
            account_id=opening.account_id, security_id=opening.security_id,
            baseline_time=opening.baseline_time, quantity=opening.quantity,
            average_cost=opening.average_cost, source=opening.source,
            confirmed_by=opening.confirmed_by,
            evidence_ids=[str(item) for item in opening.evidence_ids],
            content_hash=canonical_hash(content),
        ))
        await self._session.flush()
        await self.rebuild_position(opening.account_id, opening.security_id)
        return opening.opening_position_id

    async def add_adjustment(self, adjustment: PortfolioAdjustmentCreate) -> UUID:
        payload = adjustment.model_dump(mode="json")
        confirmed_at = datetime.now(timezone.utc) if adjustment.confirmation_status.value == "CONFIRMED" else None
        self._session.add(PortfolioAdjustmentModel(
            portfolio_adjustment_id=adjustment.portfolio_adjustment_id,
            account_id=adjustment.account_id, security_id=adjustment.security_id,
            corporate_action_id=adjustment.corporate_action_id,
            adjustment_type=adjustment.adjustment_type.value,
            effective_time=adjustment.effective_time,
            quantity_delta=adjustment.quantity_delta, cash_delta=adjustment.cash_delta,
            cost_basis_delta=adjustment.cost_basis_delta, currency=adjustment.currency,
            source=adjustment.source, source_reference=adjustment.source_reference,
            known_at=adjustment.known_at,
            confirmation_status=adjustment.confirmation_status.value,
            confirmed_by=adjustment.confirmed_by, confirmed_at=confirmed_at,
            supersedes_adjustment_id=adjustment.supersedes_adjustment_id,
            content_hash=canonical_hash(payload),
        ))
        await self._session.flush()
        if adjustment.confirmation_status.value == "CONFIRMED":
            await self.rebuild_position(adjustment.account_id, adjustment.security_id)
        return adjustment.portfolio_adjustment_id

    async def add_trade_correction(self, correction: TradeCorrectionCreate) -> UUID:
        trade = await self._session.get(TradeLedgerModel, correction.trade_id)
        if trade is None:
            raise RepositoryNotFoundError("trade not found")
        payload = correction.model_dump(mode="json")
        self._session.add(TradeCorrectionModel(
            correction_id=correction.correction_id, trade_id=correction.trade_id,
            correction_type=correction.correction_type,
            replacement=correction.replacement, reason=correction.reason,
            confirmed_by=correction.confirmed_by, content_hash=canonical_hash(payload),
        ))
        await self._session.flush()
        await self.rebuild_position(trade.account_id, trade.security_id)
        return correction.correction_id

    async def add_reconciliation(self, command: ReconciliationCreate) -> UUID:
        payload = command.model_dump(mode="json")
        self._session.add(ReconciliationModel(
            reconciliation_id=command.reconciliation_id,
            account_id=command.account_id, security_id=command.security_id,
            reconciled_at=command.reconciled_at,
            broker_facts=command.broker_facts, projected_facts=command.projected_facts,
            difference=command.difference, reason=command.reason,
            resolution=command.resolution,
            evidence_ids=[str(item) for item in command.evidence_ids],
            confirmed_by=command.confirmed_by, content_hash=canonical_hash(payload),
        ))
        return command.reconciliation_id

    async def add_preference(self, command: PortfolioPreferenceCreate) -> UUID:
        self._session.add(PortfolioPreferenceModel(
            preference_id=command.preference_id, account_id=command.account_id,
            version=command.version, preferences=command.preferences,
            effective_from=command.effective_from,
            content_hash=canonical_hash(command.model_dump(mode="json")),
        ))
        return command.preference_id

    async def rebuild_position(self, account_id: UUID, security_id: UUID) -> dict[str, Any]:
        opening = await self._session.scalar(select(OpeningPositionModel).where(
            OpeningPositionModel.account_id == account_id,
            OpeningPositionModel.security_id == security_id,
        ).order_by(OpeningPositionModel.baseline_time.desc()).limit(1))
        quantity = opening.quantity if opening else Decimal("0")
        cost_basis = quantity * opening.average_cost if opening else Decimal("0")
        cash_impact = Decimal("0")
        realized_pnl = Decimal("0")
        last_ledger = 0
        trades = (await self._session.scalars(select(TradeLedgerModel).where(
            TradeLedgerModel.account_id == account_id,
            TradeLedgerModel.security_id == security_id,
        ).order_by(TradeLedgerModel.ledger_sequence))).all()
        corrections = (await self._session.scalars(select(TradeCorrectionModel).where(
            TradeCorrectionModel.trade_id.in_([item.trade_id for item in trades])
        ).order_by(TradeCorrectionModel.created_at))).all() if trades else []
        correction_by_trade = {item.trade_id: item for item in corrections}
        for trade in trades:
            last_ledger = max(last_ledger, trade.ledger_sequence)
            correction = correction_by_trade.get(trade.trade_id)
            if correction is not None and correction.correction_type == "REVERSE":
                continue
            replacement = correction.replacement if correction is not None else {}
            side = str(replacement.get("side", trade.side))
            trade_quantity = Decimal(str(replacement.get("quantity", trade.quantity)))
            trade_price = Decimal(str(replacement.get("price", trade.price)))
            trade_fee = Decimal(str(replacement.get("fee", trade.fee)))
            if side == "BUY":
                quantity += trade_quantity
                cost_basis += trade_price * trade_quantity + trade_fee
                cash_impact -= trade_price * trade_quantity + trade_fee
            else:
                average = cost_basis / quantity if quantity else Decimal("0")
                realized_pnl += (trade_price - average) * trade_quantity - trade_fee
                cost_basis -= average * trade_quantity
                quantity -= trade_quantity
                cash_impact += trade_price * trade_quantity - trade_fee
        last_adjustment = 0
        adjustments = (await self._session.scalars(select(PortfolioAdjustmentModel).where(
            PortfolioAdjustmentModel.account_id == account_id,
            PortfolioAdjustmentModel.security_id == security_id,
            PortfolioAdjustmentModel.confirmation_status == "CONFIRMED",
        ).order_by(PortfolioAdjustmentModel.adjustment_sequence))).all()
        for item in adjustments:
            last_adjustment = max(last_adjustment, item.adjustment_sequence)
            quantity += item.quantity_delta
            cost_basis += item.cost_basis_delta
            cash_impact += item.cash_delta
        if quantity < 0:
            raise RepositoryConflictError("ledger replay produced a negative position")
        average_cost = cost_basis / quantity if quantity else Decimal("0")
        existing = await self._session.get(
            PositionProjectionModel, (account_id, security_id), with_for_update=True
        )
        inputs = {
            "opening": str(opening.content_hash) if opening else None,
            "trades": [item.content_hash for item in trades],
            "corrections": [item.content_hash for item in corrections],
            "adjustments": [item.content_hash for item in adjustments],
        }
        values = dict(
            quantity=quantity, cost_basis=cost_basis, average_cost=average_cost,
            cash_impact=cash_impact, realized_pnl=realized_pnl,
            last_ledger_sequence=last_ledger,
            last_adjustment_sequence=last_adjustment,
            rebuilt_at=datetime.now(timezone.utc), input_hash=canonical_hash(inputs),
        )
        if existing is None:
            existing = PositionProjectionModel(
                account_id=account_id, security_id=security_id,
                projection_version=1, **values,
            )
            self._session.add(existing)
        else:
            existing.projection_version += 1
            for key, value in values.items():
                setattr(existing, key, value)
        return {"account_id": account_id, "security_id": security_id, **values,
                "projection_version": existing.projection_version}

    async def position(self, account_id: UUID, security_id: UUID):
        row = await self._session.get(PositionProjectionModel, (account_id, security_id))
        if row is None:
            return None
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}
