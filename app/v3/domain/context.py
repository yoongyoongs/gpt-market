from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash
from app.v3.domain.features import FeatureRun, MarketRegimeSnapshot, SecurityFeature


CANDIDATE_COMPARISON_SCHEMA_VERSION = "candidate-comparison.v1"
CANDIDATE_COMPARISON_BUILDER_VERSION = "candidate-comparison-builder.v1"
CANDIDATE_COMPARISON_FIELD_PROFILE_VERSION = "compact-fields.v1"
CONTEXT_PACK_SCHEMA_VERSION = "context-pack.v1"
CONTEXT_PACK_BUILDER_VERSION = "context-pack-builder.v1"


class CandidateComparisonRecallHit(V3Contract):
    channel_code: str = Field(min_length=1, max_length=64)
    channel_rank: int = Field(ge=1)
    strength: float = Field(ge=0, le=1)
    reasons: tuple[str, ...]
    coverage: float = Field(ge=0, le=1)


class CandidateComparisonSourceMember(V3Contract):
    security_id: UUID
    market: str = Field(pattern=r"^(SH|SZ|BJ)$")
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=128)
    feature: SecurityFeature
    recall_hits: tuple[CandidateComparisonRecallHit, ...] = ()


class CandidateComparisonSource(V3Contract):
    feature_run: FeatureRun
    recall_run_id: UUID | None = None
    regime_snapshot_id: UUID | None = None
    members: tuple[CandidateComparisonSourceMember, ...] = Field(
        min_length=20, max_length=100
    )


class ContextBuildSource(V3Contract):
    feature_run: FeatureRun
    regime: MarketRegimeSnapshot | None = None
    recall_run_id: UUID | None = None
    market: str | None = Field(default=None, pattern=r"^(SH|SZ|BJ)$")
    code: str | None = Field(default=None, max_length=16)
    name: str | None = Field(default=None, max_length=128)
    feature: SecurityFeature | None = None


class ContextLevel(StrEnum):
    FAST = "FAST"
    NORMAL = "NORMAL"
    DEEP = "DEEP"


class EvidenceSelectionSide(StrEnum):
    SUPPORT = "SUPPORT"
    CONTRARY = "CONTRARY"
    NEUTRAL = "NEUTRAL"


class ContextSubjectType(StrEnum):
    SECURITY = "SECURITY"
    MARKET = "MARKET"


class CandidateComparisonMember(V3Contract):
    security_id: UUID
    candidate_order: int = Field(ge=1, le=100)
    market: str = Field(pattern=r"^(SH|SZ|BJ)$")
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=128)
    recall_summary: dict[str, Any] = Field(default_factory=dict)
    trend_summary: dict[str, Any] = Field(default_factory=dict)
    position_summary: dict[str, Any] = Field(default_factory=dict)
    volatility_summary: dict[str, Any] = Field(default_factory=dict)
    volume_price_summary: dict[str, Any] = Field(default_factory=dict)
    liquidity_summary: dict[str, Any] = Field(default_factory=dict)
    fundamental_summary: dict[str, Any] = Field(default_factory=dict)
    risk_summary: dict[str, Any] = Field(default_factory=dict)
    evidence_summary: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    coverage: float = Field(ge=0, le=1)
    stale: bool
    missing_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def reject_unified_final_score(self) -> "CandidateComparisonMember":
        forbidden = {"final_total_score", "action_total_score", "final_rank_score"}

        def nested_keys(value: Any):
            if isinstance(value, dict):
                for key, nested in value.items():
                    yield str(key)
                    yield from nested_keys(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    yield from nested_keys(nested)

        if forbidden.intersection(nested_keys(self.model_dump())):
            raise ValueError("candidate comparison member cannot contain a unified final score")
        return self


class CandidateComparisonPack(V3Contract):
    comparison_pack_id: UUID = Field(default_factory=uuid4)
    candidate_set_id: UUID
    builder_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=64)
    field_profile_version: str = Field(min_length=1, max_length=64)
    universe_snapshot_id: UUID
    feature_run_id: UUID
    recall_run_id: UUID | None = None
    regime_snapshot_id: UUID | None = None
    as_of: datetime
    known_at: datetime
    coverage: float = Field(ge=0, le=1)
    missing_summary: dict[str, Any] = Field(default_factory=dict)
    trim_summary: dict[str, Any] = Field(default_factory=dict)
    members: tuple[CandidateComparisonMember, ...] = Field(min_length=20, max_length=100)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("as_of", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @field_validator("coverage")
    @classmethod
    def normalize_coverage(cls, value: float) -> float:
        return round(value, 7)

    @model_validator(mode="after")
    def validate_pack(self) -> "CandidateComparisonPack":
        if self.schema_version != CANDIDATE_COMPARISON_SCHEMA_VERSION:
            raise ValueError("unsupported candidate comparison schema_version")
        if self.known_at < self.as_of:
            raise ValueError("known_at cannot be earlier than as_of")
        security_ids = [member.security_id for member in self.members]
        if len(set(security_ids)) != len(security_ids):
            raise ValueError("comparison members must have unique security_id")
        orders = [member.candidate_order for member in self.members]
        if orders != list(range(1, len(self.members) + 1)):
            raise ValueError("candidate_order must preserve a contiguous input order")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match candidate comparison pack")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(
            self.model_dump(exclude={"comparison_pack_id", "known_at", "content_hash"})
        )

    @classmethod
    def build(cls, **values: Any) -> "CandidateComparisonPack":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        payload["coverage"] = round(float(payload["coverage"]), 7)
        content_hash = canonical_hash(
            {
                key: value
                for key, value in payload.items()
                if key not in {"comparison_pack_id", "known_at"}
            }
        )
        return cls(**payload, content_hash=content_hash)


class ContextEvidenceSelection(V3Contract):
    evidence_id: UUID
    evidence_known_at: datetime
    selection_reason: str = Field(min_length=1, max_length=512)
    side: EvidenceSelectionSide
    retrieval_score: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)
    source_priority: int = Field(ge=0)
    final_order: int = Field(ge=1)

    @field_validator("evidence_known_at")
    @classmethod
    def validate_evidence_known_at(cls, value: datetime) -> datetime:
        return require_aware(value, "evidence_known_at")


class ContextPack(V3Contract):
    context_pack_id: UUID = Field(default_factory=uuid4)
    context_level: ContextLevel
    subject_type: ContextSubjectType
    subject_id: str = Field(min_length=1, max_length=128)
    task_profile_id: UUID
    task_profile_version: int = Field(ge=1)
    builder_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=64)
    as_of: datetime
    known_at: datetime
    universe_snapshot_id: UUID
    feature_run_id: UUID
    recall_run_id: UUID | None = None
    regime_snapshot_id: UUID | None = None
    comparison_pack_id: UUID | None = None
    token_budget: int = Field(ge=1)
    actual_tokens: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    missing_fields: tuple[str, ...] = ()
    trim_summary: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any]
    references: tuple[dict[str, Any], ...] = ()
    evidence_selections: tuple[ContextEvidenceSelection, ...] = ()
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("as_of", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @field_validator("coverage")
    @classmethod
    def normalize_coverage(cls, value: float) -> float:
        return round(value, 7)

    @model_validator(mode="after")
    def validate_pack(self) -> "ContextPack":
        if self.schema_version != CONTEXT_PACK_SCHEMA_VERSION:
            raise ValueError("unsupported context pack schema_version")
        if self.known_at < self.as_of:
            raise ValueError("known_at cannot be earlier than as_of")
        budget_ranges = {
            ContextLevel.FAST: (2_000, 4_000),
            ContextLevel.NORMAL: (5_000, 8_000),
            ContextLevel.DEEP: (10_000, 14_000),
        }
        minimum, maximum = budget_ranges[self.context_level]
        if not minimum <= self.token_budget <= maximum:
            raise ValueError(f"token_budget is outside {self.context_level} range")
        if self.actual_tokens > self.token_budget:
            raise ValueError("actual_tokens cannot exceed token_budget")
        evidence_ids = [selection.evidence_id for selection in self.evidence_selections]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("evidence selections must have unique evidence_id")
        orders = [selection.final_order for selection in self.evidence_selections]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("evidence final_order must be contiguous")
        if any(selection.evidence_known_at > self.as_of for selection in self.evidence_selections):
            raise ValueError("evidence_known_at cannot be later than context as_of")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match context pack")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(
            self.model_dump(exclude={"context_pack_id", "known_at", "content_hash"})
        )

    @classmethod
    def build(cls, **values: Any) -> "ContextPack":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        payload["coverage"] = round(float(payload["coverage"]), 7)
        content_hash = canonical_hash(
            {
                key: value
                for key, value in payload.items()
                if key not in {"context_pack_id", "known_at"}
            }
        )
        return cls(**payload, content_hash=content_hash)
