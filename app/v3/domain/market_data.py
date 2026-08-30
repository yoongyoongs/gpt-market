from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


class Market(StrEnum):
    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"


class UniverseSnapshotStatus(StrEnum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    LKG = "LKG"


class AdjustType(StrEnum):
    RAW = "RAW"
    QFQ = "QFQ"
    HFQ = "HFQ"


class BarPeriod(StrEnum):
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


class PointInTimePrecision(StrEnum):
    FULL = "FULL"
    LIMITED = "LIMITED"


class IngestionRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CorporateActionType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DISTRIBUTION = "STOCK_DISTRIBUTION"
    CASH_AND_STOCK_DISTRIBUTION = "CASH_AND_STOCK_DISTRIBUTION"
    OTHER_DISTRIBUTION = "OTHER_DISTRIBUTION"


class BarIngestionTarget(V3Contract):
    security_id: UUID
    code: str = Field(pattern=r"^\d{6}$")
    market: Market
    suspended: bool = False
    is_new_listing: bool = False


class MarketDataIngestionRun(V3Contract):
    run_id: UUID
    run_type: str = Field(min_length=1, max_length=64)
    universe_snapshot_id: UUID
    status: IngestionRunStatus
    cursor: dict[str, Any] = Field(default_factory=dict)
    expected_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    successful_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    errors: tuple[dict[str, Any], ...] = ()
    started_at: datetime | None = None
    completed_at: datetime | None = None
    row_version: int = Field(ge=1)

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_optional_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_counts(self) -> "MarketDataIngestionRun":
        if self.processed_count != self.successful_count + self.failed_count:
            raise ValueError("processed_count must equal successful_count + failed_count")
        if self.processed_count > self.expected_count:
            raise ValueError("processed_count cannot exceed expected_count")
        return self


class AdjustmentFactorPoint(V3Contract):
    trading_time: datetime
    factor: float = Field(gt=0)

    @field_validator("trading_time")
    @classmethod
    def validate_trading_time(cls, value: datetime) -> datetime:
        return require_aware(value, "trading_time")

    @field_validator("factor")
    @classmethod
    def normalize_factor_precision(cls, value: float) -> float:
        return round(value, 12)


class AdjustmentFactorRevisionContent(V3Contract):
    factor_revision_id: UUID
    security_id: UUID
    source: str = Field(min_length=1, max_length=128)
    upstream_source: str = Field(min_length=1, max_length=128)
    derivation_method: str = Field(min_length=1, max_length=64)
    fetch_time: datetime
    known_at: datetime
    supersedes_revision_id: UUID | None = None
    factors: tuple[AdjustmentFactorPoint, ...]

    @field_validator("fetch_time", "known_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_revision(self) -> "AdjustmentFactorRevisionContent":
        if self.known_at < self.fetch_time:
            raise ValueError("known_at cannot be earlier than fetch_time")
        times = [item.trading_time for item in self.factors]
        if not times or times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("factor times must be nonempty, increasing and unique")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(self)


class AdjustmentFactorRevision(AdjustmentFactorRevisionContent):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, content: AdjustmentFactorRevisionContent) -> "AdjustmentFactorRevision":
        return cls(**content.model_dump(), content_hash=content.computed_content_hash())

    @model_validator(mode="after")
    def validate_hash(self) -> "AdjustmentFactorRevision":
        content = AdjustmentFactorRevisionContent.model_validate(
            self.model_dump(exclude={"content_hash"})
        )
        if content.computed_content_hash() != self.content_hash:
            raise ValueError("content_hash does not match factor revision")
        return self


class CorporateActionDraft(V3Contract):
    code: str = Field(pattern=r"^\d{6}$")
    market: Market
    action_type: CorporateActionType
    announcement_time: datetime | None = None
    record_time: datetime | None = None
    effective_time: datetime
    payload: dict[str, Any]
    source: str = Field(min_length=1, max_length=128)
    source_reference: str = Field(min_length=1)
    fetch_time: datetime

    @field_validator(
        "announcement_time", "record_time", "effective_time", "fetch_time"
    )
    @classmethod
    def validate_action_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else require_aware(value, info.field_name)


class CorporateActionFetchResult(V3Contract):
    source_code: str = Field(min_length=1, max_length=128)
    fetch_time: datetime
    actions: tuple[CorporateActionDraft, ...]

    @field_validator("fetch_time")
    @classmethod
    def validate_fetch_time(cls, value: datetime) -> datetime:
        return require_aware(value, "fetch_time")

    @model_validator(mode="after")
    def validate_actions(self) -> "CorporateActionFetchResult":
        references = [action.source_reference for action in self.actions]
        if len(references) != len(set(references)):
            raise ValueError("corporate action source references must be unique")
        if any(action.source != self.source_code for action in self.actions):
            raise ValueError("corporate action source_code does not match action source")
        return self


class CorporateActionContent(V3Contract):
    corporate_action_id: UUID
    security_id: UUID
    action_type: CorporateActionType
    announcement_time: datetime | None = None
    record_time: datetime | None = None
    effective_time: datetime
    payload: dict[str, Any]
    source: str = Field(min_length=1, max_length=128)
    source_reference: str = Field(min_length=1)
    evidence_id: UUID | None = None
    fetch_time: datetime
    known_at: datetime
    supersedes_action_id: UUID | None = None

    @field_validator(
        "announcement_time",
        "record_time",
        "effective_time",
        "fetch_time",
        "known_at",
    )
    @classmethod
    def validate_corporate_action_datetimes(
        cls, value: datetime | None, info
    ) -> datetime | None:
        return None if value is None else require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_corporate_action(self) -> "CorporateActionContent":
        if self.known_at < self.fetch_time:
            raise ValueError("known_at cannot be earlier than fetch_time")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(
            self.model_dump(
                exclude={
                    "corporate_action_id",
                    "fetch_time",
                    "known_at",
                    "supersedes_action_id",
                }
            )
        )


class CorporateAction(CorporateActionContent):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, content: CorporateActionContent) -> "CorporateAction":
        return cls(**content.model_dump(), content_hash=content.computed_content_hash())

    @model_validator(mode="after")
    def validate_content_hash(self) -> "CorporateAction":
        content = CorporateActionContent.model_validate(
            self.model_dump(exclude={"content_hash"})
        )
        if content.computed_content_hash() != self.content_hash:
            raise ValueError("content_hash does not match corporate action")
        return self


class SecurityMember(V3Contract):
    code: str = Field(pattern=r"^\d{6}$")
    market: Market
    name: str = Field(min_length=1, max_length=128)
    trading_status: str = Field(default="UNKNOWN", max_length=32)
    is_st: bool = False
    suspended: bool = False
    is_new_listing: bool = False
    delisting_risk: bool = False
    raw_reference: dict[str, Any] = Field(default_factory=dict)


class UniverseFetchResult(V3Contract):
    source_code: str = Field(min_length=1, max_length=64)
    as_of: datetime
    fetch_time: datetime
    expected_total: int = Field(gt=0)
    members: tuple[SecurityMember, ...]

    @field_validator("as_of", "fetch_time")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_result(self) -> "UniverseFetchResult":
        if not self.members:
            raise ValueError("universe provider returned no members")
        keys = [(member.market, member.code) for member in self.members]
        if len(keys) != len(set(keys)):
            raise ValueError("universe provider returned duplicate members")
        return self


class UniverseSnapshotContent(V3Contract):
    snapshot_id: UUID
    source_code: str = Field(min_length=1, max_length=64)
    status: UniverseSnapshotStatus
    as_of: datetime
    fetch_time: datetime
    known_at: datetime
    coverage: float = Field(ge=0, le=1)
    stale: bool
    previous_snapshot_id: UUID | None = None
    members: tuple[SecurityMember, ...]

    @field_validator("as_of", "fetch_time", "known_at")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @field_validator("members")
    @classmethod
    def canonicalize_members(
        cls, value: tuple[SecurityMember, ...]
    ) -> tuple[SecurityMember, ...]:
        return tuple(sorted(value, key=lambda member: (member.market.value, member.code)))

    @field_validator("coverage")
    @classmethod
    def normalize_coverage_precision(cls, value: float) -> float:
        return round(value, 5)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "UniverseSnapshotContent":
        if self.known_at < self.fetch_time:
            raise ValueError("known_at cannot be earlier than fetch_time")
        keys = [(member.market, member.code) for member in self.members]
        if len(keys) != len(set(keys)):
            raise ValueError("universe members must be unique by market and code")
        if not self.members:
            raise ValueError("universe snapshot cannot be empty")
        if self.status is UniverseSnapshotStatus.LKG and not self.stale:
            raise ValueError("LKG universe snapshot must be stale")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(self)


class UniverseSnapshot(UniverseSnapshotContent):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, content: UniverseSnapshotContent) -> "UniverseSnapshot":
        return cls(**content.model_dump(), content_hash=content.computed_content_hash())

    @model_validator(mode="after")
    def validate_hash(self) -> "UniverseSnapshot":
        content = UniverseSnapshotContent.model_validate(self.model_dump(exclude={"content_hash"}))
        if content.computed_content_hash() != self.content_hash:
            raise ValueError("content_hash does not match universe snapshot")
        return self


class MarketBar(V3Contract):
    bar_time: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    amount: float | None = Field(default=None, ge=0)
    provisional: bool = False
    fetch_time: datetime

    @field_validator("bar_time", "fetch_time")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @field_validator("open", "high", "low", "close")
    @classmethod
    def normalize_price_precision(cls, value: float) -> float:
        return round(value, 6)

    @field_validator("amount")
    @classmethod
    def normalize_amount_precision(cls, value: float | None) -> float | None:
        return None if value is None else round(value, 4)

    @model_validator(mode="after")
    def validate_ohlc(self) -> "MarketBar":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be the greatest OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be the smallest OHLC value")
        return self


class HistoricalBarFetchResult(V3Contract):
    source_code: str = Field(min_length=1, max_length=128)
    upstream_source: str = Field(min_length=1, max_length=128)
    code: str = Field(pattern=r"^\d{6}$")
    period: BarPeriod
    adjust_type: AdjustType
    fetch_time: datetime
    bars: tuple[MarketBar, ...]

    @field_validator("fetch_time")
    @classmethod
    def validate_fetch_time(cls, value: datetime) -> datetime:
        return require_aware(value, "fetch_time")

    @model_validator(mode="after")
    def validate_result(self) -> "HistoricalBarFetchResult":
        times = [bar.bar_time for bar in self.bars]
        if not times or times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("fetched bar times must be nonempty, increasing and unique")
        return self


class BarSeriesRevisionContent(V3Contract):
    revision_id: UUID
    security_id: UUID
    period: BarPeriod
    adjust_type: AdjustType
    source: str = Field(min_length=1, max_length=128)
    upstream_source: str = Field(min_length=1, max_length=128)
    raw_bar_available: bool
    factor_revision_id: UUID | None = None
    point_in_time_precision: PointInTimePrecision
    precision_reason: str | None = Field(default=None, max_length=512)
    known_at: datetime
    supersedes_revision_id: UUID | None = None
    bars: tuple[MarketBar, ...]

    @field_validator("known_at")
    @classmethod
    def validate_known_at(cls, value: datetime) -> datetime:
        return require_aware(value, "known_at")

    @model_validator(mode="after")
    def validate_revision(self) -> "BarSeriesRevisionContent":
        if not self.bars:
            raise ValueError("bar series revision cannot be empty")
        times = [bar.bar_time for bar in self.bars]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("bar times must be strictly increasing and unique")
        if any(bar.fetch_time > self.known_at for bar in self.bars):
            raise ValueError("known_at cannot be earlier than bar fetch_time")
        if any(bar.provisional for bar in self.bars):
            raise ValueError("published bar revision cannot contain provisional bars")
        if self.adjust_type is AdjustType.RAW and not self.raw_bar_available:
            raise ValueError("RAW revision requires raw_bar_available")
        if self.point_in_time_precision is PointInTimePrecision.LIMITED and not self.precision_reason:
            raise ValueError("LIMITED precision requires precision_reason")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(self)


class BarSeriesRevision(BarSeriesRevisionContent):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, content: BarSeriesRevisionContent) -> "BarSeriesRevision":
        return cls(**content.model_dump(), content_hash=content.computed_content_hash())

    @model_validator(mode="after")
    def validate_hash(self) -> "BarSeriesRevision":
        content = BarSeriesRevisionContent.model_validate(self.model_dump(exclude={"content_hash"}))
        if content.computed_content_hash() != self.content_hash:
            raise ValueError("content_hash does not match bar series revision")
        return self
