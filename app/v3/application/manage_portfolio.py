from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import Field

from app.v3.contracts.base import V3Contract
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


class PortfolioWriteService:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

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

    async def confirm_trade(self, draft_id: UUID, command: TradeConfirm):
        async with self._uow_factory() as uow:
            trade_id = await uow.portfolios.confirm_trade(draft_id, command)
            await uow.commit()
        return {"trade_id": trade_id, "status": "CONFIRMED"}

    async def add_opening(self, command: OpeningPositionCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.portfolios.add_opening_position(command)
            await uow.commit()
        return {"opening_position_id": object_id, "status": "CONFIRMED_BASELINE"}

    async def confirm_position_draft(self, draft_id: UUID, command: DraftConfirmation):
        async with self._uow_factory() as uow:
            opening_id = await uow.portfolios.confirm_position_draft(
                draft_id, command.confirmed_by
            )
            await uow.commit()
        return {"opening_position_id": opening_id, "status": "CONFIRMED_BASELINE"}

    async def add_adjustment(self, command: PortfolioAdjustmentCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.portfolios.add_adjustment(command)
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

    async def add_trade_correction(self, command: TradeCorrectionCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.portfolios.add_trade_correction(command)
            await uow.commit()
        return {"correction_id": object_id, "status": "APPENDED"}

    async def add_reconciliation(self, command: ReconciliationCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.portfolios.add_reconciliation(command)
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
