from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class DraftStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class AdjustmentConfirmation(StrEnum):
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    CONFIRMED = "CONFIRMED"
    REVERSED = "REVERSED"


class AdjustmentType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    CAPITALIZATION = "CAPITALIZATION"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    OTHER = "OTHER"


class AccountCreate(V3Contract):
    account_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=128)
    currency: str = Field(default="CNY", min_length=3, max_length=8)
    cost_method: str = Field(default="WEIGHTED_AVERAGE", min_length=1, max_length=32)


class TradeDraftCreate(V3Contract):
    draft_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    security_id: UUID
    side: TradeSide
    trade_time: datetime
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    fee: Decimal = Field(default=Decimal("0"), ge=0)
    decision_id: UUID | None = None
    entry_plan_id: UUID | None = None
    entry_plan_version: int | None = Field(default=None, ge=1)
    source: str = Field(default="MANUAL", min_length=1, max_length=64)
    source_reference: str | None = Field(default=None, max_length=512)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    image_import_id: UUID | None = None

    @field_validator("trade_time")
    @classmethod
    def validate_trade_time(cls, value: datetime) -> datetime:
        return require_aware(value, "trade_time")

    @model_validator(mode="after")
    def validate_plan_binding(self) -> "TradeDraftCreate":
        if (self.entry_plan_id is None) != (self.entry_plan_version is None):
            raise ValueError("entry plan id and version must be supplied together")
        return self


class TradeConfirm(V3Contract):
    idempotency_key: str = Field(min_length=16, max_length=128)
    confirmed_by: str = Field(min_length=1, max_length=128)


class OpeningPositionCreate(V3Contract):
    opening_position_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    security_id: UUID
    baseline_time: datetime
    quantity: Decimal = Field(ge=0)
    average_cost: Decimal = Field(ge=0)
    source: str = Field(min_length=1, max_length=64)
    confirmed_by: str = Field(min_length=1, max_length=128)
    evidence_ids: tuple[UUID, ...] = ()

    @field_validator("baseline_time")
    @classmethod
    def validate_baseline_time(cls, value: datetime) -> datetime:
        return require_aware(value, "baseline_time")


class PositionSnapshotDraftCreate(V3Contract):
    draft_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    security_id: UUID
    as_of: datetime
    quantity: Decimal = Field(ge=0)
    average_cost: Decimal = Field(ge=0)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    image_import_id: UUID | None = None

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class TradeCorrectionCreate(V3Contract):
    correction_id: UUID = Field(default_factory=uuid4)
    trade_id: UUID
    correction_type: str = Field(pattern=r"^(REVERSE|CORRECT)$")
    replacement: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=1024)
    confirmed_by: str = Field(min_length=1, max_length=128)


class ReconciliationCreate(V3Contract):
    reconciliation_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    security_id: UUID
    reconciled_at: datetime
    broker_facts: dict[str, Any]
    projected_facts: dict[str, Any]
    difference: dict[str, Any]
    reason: str = "UNKNOWN"
    resolution: str = "UNKNOWN"
    evidence_ids: tuple[UUID, ...] = ()
    confirmed_by: str

    @field_validator("reconciled_at")
    @classmethod
    def validate_reconciled_at(cls, value: datetime) -> datetime:
        return require_aware(value, "reconciled_at")


class PortfolioPreferenceCreate(V3Contract):
    preference_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    version: int = Field(ge=1)
    preferences: dict[str, Any]
    effective_from: datetime

    @field_validator("effective_from")
    @classmethod
    def validate_preference_time(cls, value: datetime) -> datetime:
        return require_aware(value, "effective_from")


class PortfolioAdjustmentCreate(V3Contract):
    portfolio_adjustment_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    security_id: UUID
    corporate_action_id: UUID | None = None
    adjustment_type: AdjustmentType
    effective_time: datetime
    quantity_delta: Decimal = Decimal("0")
    cash_delta: Decimal = Decimal("0")
    cost_basis_delta: Decimal = Decimal("0")
    currency: str = "CNY"
    source: str = "UNKNOWN"
    source_reference: str = "UNKNOWN"
    known_at: datetime
    confirmation_status: AdjustmentConfirmation = AdjustmentConfirmation.PENDING_RECONCILIATION
    confirmed_by: str | None = None
    supersedes_adjustment_id: UUID | None = None

    @field_validator("effective_time", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)


class PositionProjection(V3Contract):
    account_id: UUID
    security_id: UUID
    quantity: Decimal
    cost_basis: Decimal
    average_cost: Decimal
    cash_impact: Decimal
    realized_pnl: Decimal
    last_ledger_sequence: int = Field(ge=0)
    last_adjustment_sequence: int = Field(ge=0)
    projection_version: int = Field(ge=1)
    rebuilt_at: datetime
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("rebuilt_at")
    @classmethod
    def validate_rebuilt_at(cls, value: datetime) -> datetime:
        return require_aware(value, "rebuilt_at")

    @classmethod
    def build(cls, **values: Any) -> "PositionProjection":
        payload = cls.model_construct(**values, input_hash="0" * 64).model_dump(
            exclude={"input_hash"}
        )
        return cls(**payload, input_hash=canonical_hash(payload))
