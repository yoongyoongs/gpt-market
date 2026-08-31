from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


class PerformanceAbility(StrEnum):
    SELECTION = "SELECTION"
    INITIAL_ENTRY = "INITIAL_ENTRY"
    USER_EXECUTION = "USER_EXECUTION"
    ADD = "ADD"
    REDUCE = "REDUCE"
    FINAL_EXIT = "FINAL_EXIT"
    RISK_CONTROL = "RISK_CONTROL"


class ReplayStatus(StrEnum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


class PerformanceAttributionCreate(V3Contract):
    attribution_id: UUID = Field(default_factory=uuid4)
    ability: PerformanceAbility
    subject_type: str = Field(min_length=1, max_length=32)
    subject_id: UUID
    strategy_version: str = Field(min_length=1, max_length=64)
    decision_id: UUID | None = None
    original_entry_plan_id: UUID | None = None
    evaluated_entry_plan_id: UUID | None = None
    trade_id: UUID | None = None
    trade_bound_entry_plan_id: UUID | None = None
    regime_snapshot_id: UUID | None = None
    horizon_sessions: int = Field(ge=1)
    as_of: datetime
    matures_at: datetime
    known_at: datetime
    raw_return: float | None = None
    excess_return: float | None = None
    mfe: float | None = None
    mae: float | None = None
    target_hit: bool | None = None
    stop_hit: bool | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    explanation: str = Field(min_length=1)

    @field_validator("as_of", "matures_at", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_attribution(self) -> "PerformanceAttributionCreate":
        if self.matures_at <= self.as_of or self.known_at < self.matures_at:
            raise ValueError("performance attribution must be written after maturity")
        if self.ability is PerformanceAbility.USER_EXECUTION and self.trade_id is None:
            raise ValueError("user execution attribution requires a trade")
        if self.trade_bound_entry_plan_id is not None and self.trade_id is None:
            raise ValueError("trade-bound plan attribution requires a trade")
        return self


class ReplayRunCreate(V3Contract):
    replay_run_id: UUID = Field(default_factory=uuid4)
    strategy_version: str = Field(min_length=1, max_length=64)
    replay_as_of: datetime
    bar_revision_ids: tuple[UUID, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    context_pack_ids: tuple[UUID, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("replay_as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware(value, "replay_as_of")


class RegressionCaseCreate(V3Contract):
    regression_case_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=128)
    strategy_version: str = Field(min_length=1, max_length=64)
    replay_as_of: datetime
    input_requirements: dict[str, Any]
    expected_invariants: dict[str, Any]
    source_replay_run_id: UUID | None = None

    @field_validator("replay_as_of")
    @classmethod
    def validate_replay_time(cls, value: datetime) -> datetime:
        return require_aware(value, "replay_as_of")


def content_hash(value: V3Contract) -> str:
    return canonical_hash(value.model_dump(mode="json"))
