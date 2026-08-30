from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


def _float_fields(payload: dict[str, Any], *fields: str) -> dict[str, Any]:
    for field in fields:
        if payload.get(field) is not None:
            payload[field] = float(payload[field])
    return payload


class RecallRunStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class ObservationStatus(StrEnum):
    PENDING = "PENDING"
    MATURED = "MATURED"
    UNAVAILABLE = "UNAVAILABLE"


class RecallChannel(V3Contract):
    channel_id: UUID = Field(default_factory=uuid4)
    code: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    configuration: dict[str, Any]
    description: str = Field(min_length=1)
    enabled: bool = True
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, **values: Any) -> "RecallChannel":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        return cls(**payload, content_hash=canonical_hash(
            {key: value for key, value in payload.items() if key != "channel_id"}
        ))

    @model_validator(mode="after")
    def validate_hash(self) -> "RecallChannel":
        expected = canonical_hash(self.model_dump(exclude={"channel_id", "content_hash"}))
        if self.content_hash != expected:
            raise ValueError("content_hash does not match recall channel")
        return self


class RecallRun(V3Contract):
    recall_run_id: UUID = Field(default_factory=uuid4)
    feature_run_id: UUID
    regime_snapshot_id: UUID | None = None
    strategy_version: str = Field(min_length=1, max_length=64)
    channel_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: datetime
    known_at: datetime
    status: RecallRunStatus
    expected_channel_count: int = Field(ge=1)
    successful_channel_count: int = Field(ge=0)
    failed_channel_count: int = Field(ge=0)
    security_count: int = Field(ge=0)
    hit_security_count: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    errors: dict[str, str] = Field(default_factory=dict)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("as_of", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_run(self) -> "RecallRun":
        if self.known_at < self.as_of:
            raise ValueError("known_at cannot be earlier than as_of")
        if self.successful_channel_count + self.failed_channel_count != self.expected_channel_count:
            raise ValueError("recall channel counts must be complete")
        if self.hit_security_count > self.security_count:
            raise ValueError("hit_security_count cannot exceed security_count")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match recall run")
        return self

    def computed_content_hash(self) -> str:
        payload = self.model_dump(exclude={"recall_run_id", "content_hash"})
        return canonical_hash(_float_fields(payload, "coverage"))

    @classmethod
    def build(cls, **values: Any) -> "RecallRun":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        content_hash = canonical_hash(_float_fields({
            key: value for key, value in payload.items() if key != "recall_run_id"
        }, "coverage"))
        return cls(**payload, content_hash=content_hash)


class RecallResult(V3Contract):
    recall_result_id: UUID = Field(default_factory=uuid4)
    recall_run_id: UUID
    channel_id: UUID
    security_id: UUID
    channel_rank: int = Field(ge=1)
    strength: float = Field(ge=0, le=1)
    reasons: tuple[str, ...] = Field(min_length=1)
    matched_features: dict[str, Any]
    coverage: float = Field(ge=0, le=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, **values: Any) -> "RecallResult":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        hash_payload = {key: value for key, value in payload.items() if key != "recall_result_id"}
        return cls(**payload, content_hash=canonical_hash(
            _float_fields(hash_payload, "strength", "coverage")
        ))

    @model_validator(mode="after")
    def validate_hash(self) -> "RecallResult":
        payload = self.model_dump(exclude={"recall_result_id", "content_hash"})
        expected = canonical_hash(_float_fields(payload, "strength", "coverage"))
        if self.content_hash != expected:
            raise ValueError("content_hash does not match recall result")
        return self


class RawOpportunity(V3Contract):
    raw_opportunity_id: UUID = Field(default_factory=uuid4)
    recall_run_id: UUID
    security_id: UUID
    as_of: datetime
    known_at: datetime
    recall_result_ids: tuple[UUID, ...] = Field(min_length=1)
    channel_codes: tuple[str, ...] = Field(min_length=1)
    reason_summary: dict[str, tuple[str, ...]]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("as_of", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_raw(self) -> "RawOpportunity":
        if self.known_at < self.as_of:
            raise ValueError("known_at cannot be earlier than as_of")
        if len(set(self.recall_result_ids)) != len(self.recall_result_ids):
            raise ValueError("recall_result_ids must be unique")
        if len(set(self.channel_codes)) != len(self.channel_codes):
            raise ValueError("channel_codes must be unique")
        if set(self.reason_summary) != set(self.channel_codes):
            raise ValueError("reason_summary must cover every channel")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match raw opportunity")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(self.model_dump(exclude={"raw_opportunity_id", "content_hash"}))

    @classmethod
    def build(cls, **values: Any) -> "RawOpportunity":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        content_hash = canonical_hash({
            key: value for key, value in payload.items() if key != "raw_opportunity_id"
        })
        return cls(**payload, content_hash=content_hash)


class PerformanceObservation(V3Contract):
    observation_id: UUID = Field(default_factory=uuid4)
    recall_run_id: UUID
    security_id: UUID
    horizon_sessions: int
    status: ObservationStatus
    as_of: datetime
    matures_at: datetime
    known_at: datetime
    baseline_price: float = Field(gt=0)
    future_price: float | None = Field(default=None, gt=0)
    raw_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("horizon_sessions")
    @classmethod
    def validate_horizon(cls, value: int) -> int:
        if value not in {3, 5, 10}:
            raise ValueError("horizon_sessions must be 3, 5 or 10")
        return value

    @field_validator("as_of", "matures_at", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_observation(self) -> "PerformanceObservation":
        if self.matures_at <= self.as_of or self.known_at < self.as_of:
            raise ValueError("observation time ordering is invalid")
        if self.status is ObservationStatus.PENDING and any(
            value is not None for value in (self.future_price, self.raw_return, self.excess_return)
        ):
            raise ValueError("pending observation cannot contain future results")
        if self.status is ObservationStatus.MATURED and (
            self.known_at < self.matures_at or self.future_price is None or self.raw_return is None
        ):
            raise ValueError("matured observation requires mature time and future result")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match performance observation")
        return self

    def computed_content_hash(self) -> str:
        payload = self.model_dump(exclude={"observation_id", "content_hash"})
        return canonical_hash(_float_fields(
            payload, "baseline_price", "future_price", "raw_return",
            "benchmark_return", "excess_return",
        ))

    @classmethod
    def build(cls, **values: Any) -> "PerformanceObservation":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        content_hash = canonical_hash(_float_fields({
            key: value for key, value in payload.items() if key != "observation_id"
        }, "baseline_price", "future_price", "raw_return", "benchmark_return", "excess_return"))
        return cls(**payload, content_hash=content_hash)


class RecallMissEvaluation(V3Contract):
    evaluation_id: UUID = Field(default_factory=uuid4)
    observation_id: UUID
    threshold_version: str = Field(min_length=1, max_length=64)
    threshold_spec: dict[str, Any]
    was_recalled: bool
    is_exceptional: bool
    miss_type: str | None = Field(default=None, max_length=64)
    evaluated_at: datetime
    known_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("evaluated_at", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_evaluation(self) -> "RecallMissEvaluation":
        if self.known_at < self.evaluated_at:
            raise ValueError("known_at cannot be earlier than evaluated_at")
        is_miss = self.is_exceptional and not self.was_recalled
        if is_miss != (self.miss_type is not None):
            raise ValueError("miss_type is required only for exceptional unrecalled observations")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match recall miss evaluation")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(self.model_dump(exclude={"evaluation_id", "content_hash"}))

    @classmethod
    def build(cls, **values: Any) -> "RecallMissEvaluation":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        content_hash = canonical_hash({
            key: value for key, value in payload.items() if key != "evaluation_id"
        })
        return cls(**payload, content_hash=content_hash)
