from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import Field

from app.v3.application.audit_helper import AuditRecorder
from app.v3.application.read_position_context import ReadPositionContextService
from app.v3.contracts.base import V3Contract
from app.v3.repositories.errors import (
    RepositoryNotFoundError,
)
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


class ImageDraftImport(V3Contract):
    image_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_reference: str = Field(min_length=1, max_length=2048)
    import_type: str = Field(pattern=r"^(TRADE|POSITION)$")
    ocr_payload: dict[str, Any]
    field_regions: dict[str, Any] = Field(default_factory=dict)
    trade_drafts: tuple[TradeDraftCreate, ...] = ()
    position_drafts: tuple[PositionSnapshotDraftCreate, ...] = ()


class DraftConfirmation(V3Contract):
    confirmed_by: str = Field(min_length=1, max_length=128)


class DraftFieldCorrection(V3Contract):
    """用户对 OCR Draft 字段的人工修正（PG-002）。"""

    corrected_fields: dict[str, Any]
    corrected_by: str = Field(min_length=1, max_length=128)


_TRADE_DRAFT_FIELDS = {"side", "price", "quantity", "fee", "trade_time"}
_POSITION_DRAFT_FIELDS = {"quantity", "average_cost", "as_of"}


def _validate_draft_correction(corrected_fields: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(corrected_fields) - allowed)
    if unknown:
        raise ValueError(f"unsupported draft fields: {', '.join(unknown)}")
    from datetime import datetime as _dt
    from decimal import Decimal as _Decimal, InvalidOperation as _Invalid

    if "side" in corrected_fields and corrected_fields["side"] not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    for key in ("price", "quantity", "fee", "average_cost"):
        if key not in corrected_fields:
            continue
        try:
            value = _Decimal(str(corrected_fields[key]))
        except _Invalid as exc:
            raise ValueError(f"{key} is not a valid number") from exc
        if value < 0:
            raise ValueError(f"{key} must be non-negative")
    for key in ("trade_time", "as_of"):
        if key not in corrected_fields:
            continue
        try:
            _dt.fromisoformat(str(corrected_fields[key]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{key} is not a valid ISO datetime") from exc


def _actor_id(command) -> str | None:
    for field in ("confirmed_by", "corrected_by", "actor_id", "created_by"):
        value = getattr(command, field, None)
        if value:
            return str(value)
    return None


class PortfolioWriteService:
    """RC-08A：关键 WRITE 同事务追加 AuditEvent（业务写入 → 审计 → commit）。"""

    def __init__(self, uow_factory, *, clock=None) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def _recorder(self, uow) -> AuditRecorder:
        return AuditRecorder(uow, clock=self._clock)

    async def create_account(self, command: AccountCreate):
        async with self._uow_factory() as uow:
            account_id = await uow.portfolios.add_account(command)
            await uow.commit()
        return {"account_id": account_id, "status": "ACTIVE"}

    async def create_trade_draft(self, command: TradeDraftCreate):
        async with self._uow_factory() as uow:
            draft_id = await uow.portfolios.add_trade_draft(command)
            await uow.commit()
        return {"draft_id": draft_id, "status": "DRAFT"}

    async def import_image_drafts(self, command: ImageDraftImport):
        async with self._uow_factory() as uow:
            image_import_id = await uow.portfolios.add_image_import(
                command.image_hash, command.image_reference, command.import_type,
                command.ocr_payload, command.field_regions,
            )
            draft_ids = []
            for draft in command.trade_drafts:
                with_image = draft.model_copy(update={"image_import_id": image_import_id})
                draft_ids.append(await uow.portfolios.add_trade_draft(with_image))
            position_draft_ids = []
            for draft in command.position_drafts:
                with_image = draft.model_copy(update={"image_import_id": image_import_id})
                position_draft_ids.append(await uow.portfolios.add_position_draft(with_image))
            await uow.commit()
        return {
            "image_import_id": image_import_id,
            "draft_ids": tuple(draft_ids),
            "position_draft_ids": tuple(position_draft_ids),
            "status": "DRAFT_ONLY",
            "requires_manual_confirmation": True,
        }

    async def trade_draft_preview(self, draft_id: UUID):
        async with self._uow_factory() as uow:
            preview = await uow.portfolios.get_trade_draft(draft_id)
        if preview is None:
            raise RepositoryNotFoundError("trade draft not found")
        return preview

    async def position_draft_preview(self, draft_id: UUID):
        async with self._uow_factory() as uow:
            preview = await uow.portfolios.get_position_draft(draft_id)
        if preview is None:
            raise RepositoryNotFoundError("position snapshot draft not found")
        return preview

    async def correct_trade_draft(self, draft_id: UUID, command: DraftFieldCorrection,
                                  *, request_id: str | None = None):
        _validate_draft_correction(command.corrected_fields, _TRADE_DRAFT_FIELDS)
        async with self._uow_factory() as uow:
            report = await uow.portfolios.correct_trade_draft(
                draft_id, command.corrected_fields, command.corrected_by
            )
            if report is None:
                raise RepositoryNotFoundError("trade draft not found")
            await self._recorder(uow).record(
                action="TRADE_DRAFT_CORRECTED", object_type="TRADE_DRAFT",
                object_id=str(draft_id), actor_id=command.corrected_by,
                request_id=request_id, after=command,
                metadata={"corrected_fields": sorted(command.corrected_fields)},
            )
            await uow.commit()
        return report

    async def correct_position_draft(self, draft_id: UUID, command: DraftFieldCorrection,
                                     *, request_id: str | None = None):
        _validate_draft_correction(command.corrected_fields, _POSITION_DRAFT_FIELDS)
        async with self._uow_factory() as uow:
            report = await uow.portfolios.correct_position_draft(
                draft_id, command.corrected_fields, command.corrected_by
            )
            if report is None:
                raise RepositoryNotFoundError("position snapshot draft not found")
            await self._recorder(uow).record(
                action="POSITION_DRAFT_CORRECTED", object_type="POSITION_SNAPSHOT_DRAFT",
                object_id=str(draft_id), actor_id=command.corrected_by,
                request_id=request_id, after=command,
                metadata={"corrected_fields": sorted(command.corrected_fields)},
            )
            await uow.commit()
        return report

    async def confirm_trade(self, draft_id: UUID, command: TradeConfirm,
                            *, request_id: str | None = None):
        async with self._uow_factory() as uow:
            trade_id = await uow.portfolios.confirm_trade(draft_id, command)
            await self._recorder(uow).record(
                action="TRADE_CONFIRMED", object_type="TRADE",
                object_id=str(trade_id), actor_id=_actor_id(command),
                request_id=request_id, after=command,
                metadata={"trade_draft_id": str(draft_id),
                          "idempotency_key": command.idempotency_key},
            )
            await uow.commit()
        return {"trade_id": trade_id, "status": "CONFIRMED"}

    async def add_opening(self, command: OpeningPositionCreate,
                          *, request_id: str | None = None):
        async with self._uow_factory() as uow:
            object_id = await uow.portfolios.add_opening_position(command)
            await self._recorder(uow).record(
                action="OPENING_POSITION_ADDED", object_type="OPENING_POSITION",
                object_id=str(object_id), actor_id=_actor_id(command),
                request_id=request_id, after=command,
            )
            await uow.commit()
        return {"opening_position_id": object_id, "status": "CONFIRMED_BASELINE"}

    async def confirm_position_draft(self, draft_id: UUID, command: DraftConfirmation,
                                     *, request_id: str | None = None):
        async with self._uow_factory() as uow:
            opening_id = await uow.portfolios.confirm_position_draft(
                draft_id, command.confirmed_by
            )
            await self._recorder(uow).record(
                action="OPENING_POSITION_CONFIRMED",
                object_type="OPENING_POSITION", object_id=str(opening_id),
                actor_id=command.confirmed_by, request_id=request_id,
                after=command,
                metadata={"position_snapshot_draft_id": str(draft_id)},
            )
            await uow.commit()
        return {"opening_position_id": opening_id, "status": "CONFIRMED_BASELINE"}

    async def add_adjustment(self, command: PortfolioAdjustmentCreate,
                             *, request_id: str | None = None):
        async with self._uow_factory() as uow:
            object_id = await uow.portfolios.add_adjustment(command)
            await self._recorder(uow).record(
                action="PORTFOLIO_ADJUSTMENT_APPENDED",
                object_type="PORTFOLIO_ADJUSTMENT", object_id=str(object_id),
                actor_id=_actor_id(command), request_id=request_id,
                after=command,
            )
            await uow.commit()
        return {
            "portfolio_adjustment_id": object_id,
            "status": command.confirmation_status,
        }

    async def rebuild_position(self, account_id: UUID, security_id: UUID):
        async with self._uow_factory() as uow:
            projection = await uow.portfolios.rebuild_position(account_id, security_id)
            await uow.commit()
        return projection

    async def add_trade_correction(self, command: TradeCorrectionCreate,
                                   *, request_id: str | None = None):
        async with self._uow_factory() as uow:
            object_id = await uow.portfolios.add_trade_correction(command)
            await self._recorder(uow).record(
                action="TRADE_CORRECTION_APPENDED", object_type="TRADE_CORRECTION",
                object_id=str(object_id), actor_id=_actor_id(command),
                request_id=request_id, after=command,
            )
            await uow.commit()
        return {"correction_id": object_id, "status": "APPENDED"}

    async def add_reconciliation(self, command: ReconciliationCreate,
                                 *, request_id: str | None = None):
        async with self._uow_factory() as uow:
            object_id = await uow.portfolios.add_reconciliation(command)
            await self._recorder(uow).record(
                action="RECONCILIATION_APPENDED", object_type="RECONCILIATION",
                object_id=str(object_id), actor_id=_actor_id(command),
                request_id=request_id, after=command,
            )
            await uow.commit()
        return {"reconciliation_id": object_id, "status": "APPENDED"}

    async def add_preference(self, command: PortfolioPreferenceCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.portfolios.add_preference(command)
            await uow.commit()
        return {"preference_id": object_id, "status": "ACTIVE_SOFT_PREFERENCE"}

    async def read_position(self, account_id: UUID, security_id: UUID):
        async with self._uow_factory() as uow:
            return await uow.portfolios.position(account_id, security_id)

    async def read_position_context(
        self, account_id: UUID, code: str, market: str | None,
        *, deep_market_data=None, calendar=None, quote_service=None,
    ):
        # RC-05B（CTX-001）：全量载荷由专用服务组装（行情/多周期/级别/风控等）
        service = ReadPositionContextService(
            self._uow_factory,
            calendar=calendar,
            deep_market_data=deep_market_data,
            quote_service=quote_service,
        )
        return await service.execute(account_id, code, market)
