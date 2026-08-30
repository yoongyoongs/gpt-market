from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


class FeatureRunStatus(StrEnum):
    RUNNING = "RUNNING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class FeatureSortField(StrEnum):
    CODE = "code"
    RETURN_20D = "return_20d"
    RETURN_60D = "return_60d"
    POSITION_60D = "position_60d"
    AMOUNT = "amount"
    ATR_PCT = "atr_pct"
    VOLUME_RATIO_5D = "volume_ratio_5d"
    COVERAGE = "coverage"


class FeatureQuery(V3Contract):
    feature_run_id: UUID | None = None
    market: str | None = Field(default=None, pattern=r"^(SH|SZ|BJ)$")
    stale: bool | None = None
    sort_by: FeatureSortField = FeatureSortField.CODE
    descending: bool = False
    min_value: float | None = None
    max_value: float | None = None
    fields: tuple[str, ...] = ()
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class FeaturePage(V3Contract):
    feature_run_id: UUID
    as_of: datetime
    feature_version: str
    total_count: int = Field(ge=0)
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None = None
    quality_summary: dict[str, Any]

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class FeatureRun(V3Contract):
    feature_run_id: UUID
    as_of: datetime
    universe_snapshot_id: UUID
    feature_version: str = Field(min_length=1, max_length=64)
    status: FeatureRunStatus
    expected_count: int = Field(ge=0)
    successful_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    bar_revision_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_manifest: dict[str, str]
    error_summary: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("as_of", "started_at", "completed_at")
    @classmethod
    def validate_times(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_run(self) -> "FeatureRun":
        if self.successful_count + self.failed_count > self.expected_count:
            raise ValueError("feature run counts exceed expected_count")
        if self.status is FeatureRunStatus.PUBLISHED:
            if self.completed_at is None or self.successful_count + self.failed_count != self.expected_count:
                raise ValueError("published feature run must be complete")
            expected = self.computed_content_hash()
            if self.content_hash != expected:
                raise ValueError("content_hash does not match feature run")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(self.model_dump(exclude={
            "feature_run_id", "status", "started_at", "completed_at", "content_hash"
        }))

    def published(self, *, completed_at: datetime) -> "FeatureRun":
        payload = self.model_copy(
            update={"status": FeatureRunStatus.PUBLISHED, "completed_at": completed_at, "content_hash": None}
        )
        return payload.model_copy(
            update={"content_hash": payload.computed_content_hash()}
        )


class SecurityFeature(V3Contract):
    feature_run_id: UUID
    security_id: UUID
    series_revision_id: UUID
    factor_revision_id: UUID | None = None
    as_of: datetime
    close: float = Field(gt=0)
    return_3d: float | None = None
    return_5d: float | None = None
    return_10d: float | None = None
    return_20d: float | None = None
    return_60d: float | None = None
    return_120d: float | None = None
    return_250d: float | None = None
    position_60d: float | None = Field(default=None, ge=0, le=1)
    position_120d: float | None = Field(default=None, ge=0, le=1)
    position_250d: float | None = Field(default=None, ge=0, le=1)
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    ma20_slope: float | None = None
    ma60_slope: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None
    volatility20: float | None = None
    distance_60d_high: float | None = None
    distance_60d_low: float | None = None
    breakout_20d: bool | None = None
    pullback_20d: bool | None = None
    amount: float | None = Field(default=None, ge=0)
    volume_ratio_5d: float | None = Field(default=None, ge=0)
    volume_expansion: bool | None = None
    relative_index_strength: float | None = None
    relative_industry_strength: float | None = None
    coverage: float = Field(ge=0, le=1)
    stale: bool
    missing_fields: tuple[str, ...] = ()
    source_errors: tuple[str, ...] = ()
    quality: dict[str, Any] = Field(default_factory=dict)
    features: dict[str, Any] = Field(default_factory=dict)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")

    @classmethod
    def build(cls, **values: Any) -> "SecurityFeature":
        payload = cls.model_construct(**values, content_hash="0" * 64)
        content_hash = canonical_hash(payload.model_dump(exclude={"content_hash"}))
        return cls(**values, content_hash=content_hash)

    @model_validator(mode="after")
    def validate_hash(self) -> "SecurityFeature":
        if canonical_hash(self.model_dump(exclude={"content_hash"})) != self.content_hash:
            raise ValueError("content_hash does not match security feature")
        return self


class MarketRegimeSnapshot(V3Contract):
    regime_snapshot_id: UUID
    feature_run_id: UUID
    as_of: datetime
    known_at: datetime
    index_states: dict[str, Any] = Field(default_factory=dict)
    breadth: dict[str, Any]
    turnover: dict[str, Any]
    limit_structure: dict[str, Any] = Field(default_factory=dict)
    size_style: dict[str, Any] = Field(default_factory=dict)
    growth_value_style: dict[str, Any] = Field(default_factory=dict)
    industry_rotation: dict[str, Any] = Field(default_factory=dict)
    risk_appetite_facts: dict[str, Any]
    domestic_risk_evidence_ids: tuple[UUID, ...] = ()
    global_risk_evidence_ids: tuple[UUID, ...] = ()
    coverage: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    stale: bool
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("as_of", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @classmethod
    def build(cls, **values: Any) -> "MarketRegimeSnapshot":
        payload = cls.model_construct(**values, content_hash="0" * 64)
        return cls(
            **values,
            content_hash=canonical_hash(payload.model_dump(exclude={"content_hash"})),
        )

    @model_validator(mode="after")
    def validate_snapshot(self) -> "MarketRegimeSnapshot":
        if self.known_at < self.as_of:
            raise ValueError("known_at cannot be earlier than as_of")
        if canonical_hash(self.model_dump(exclude={"content_hash"})) != self.content_hash:
            raise ValueError("content_hash does not match market regime")
        return self
