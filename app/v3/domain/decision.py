from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.agent import AgentIdentity
from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


class WatchlistState(StrEnum):
    WATCHING = "WATCHING"
    WAIT_ENTRY = "WAIT_ENTRY"
    ACTION_READY = "ACTION_READY"
    TRIGGERED = "TRIGGERED"
    SLOW = "SLOW"
    DOWNGRADED = "DOWNGRADED"
    INVALIDATED = "INVALIDATED"
    HOLDING = "HOLDING"
    CLOSED = "CLOSED"


WATCHLIST_TRANSITIONS = {
    WatchlistState.WATCHING: {WatchlistState.WAIT_ENTRY, WatchlistState.SLOW, WatchlistState.INVALIDATED},
    WatchlistState.WAIT_ENTRY: {WatchlistState.ACTION_READY, WatchlistState.DOWNGRADED, WatchlistState.INVALIDATED},
    WatchlistState.ACTION_READY: {WatchlistState.TRIGGERED, WatchlistState.DOWNGRADED, WatchlistState.INVALIDATED},
    WatchlistState.TRIGGERED: {WatchlistState.HOLDING, WatchlistState.INVALIDATED},
    WatchlistState.SLOW: {WatchlistState.WATCHING, WatchlistState.INVALIDATED},
    WatchlistState.DOWNGRADED: {WatchlistState.WATCHING, WatchlistState.INVALIDATED},
    WatchlistState.INVALIDATED: {WatchlistState.WATCHING, WatchlistState.CLOSED},
    WatchlistState.HOLDING: {WatchlistState.CLOSED},
    WatchlistState.CLOSED: set(),
}


def validate_watchlist_transition(
    current: WatchlistState, target: WatchlistState, *, confirmed_position_quantity: float
) -> None:
    if target not in WATCHLIST_TRANSITIONS[current]:
        raise ValueError(f"invalid watchlist transition: {current} -> {target}")
    if target is WatchlistState.HOLDING and confirmed_position_quantity <= 0:
        raise ValueError("HOLDING requires a confirmed positive ledger position")
    if target is WatchlistState.CLOSED and confirmed_position_quantity > 0:
        raise ValueError("CLOSED requires zero confirmed ledger quantity")


class ExpectedHorizon(StrEnum):
    D1_5 = "D1_5"
    D3_10 = "D3_10"
    D10_20 = "D10_20"
    D20_60 = "D20_60"


class ThesisStatus(StrEnum):
    STRENGTHENED = "STRENGTHENED"
    MAINTAINED = "MAINTAINED"
    TIME_EFFICIENCY_DECLINING = "TIME_EFFICIENCY_DECLINING"
    WEAKENED = "WEAKENED"
    INVALIDATED = "INVALIDATED"


class TimeEfficiencyState(StrEnum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    SLOW = "SLOW"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class ImmutableDecisionFact(V3Contract):
    fact_id: UUID = Field(default_factory=uuid4)
    task_run_id: UUID
    context_pack_id: UUID
    context_pack_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent: AgentIdentity
    evidence_ids: tuple[UUID, ...] = ()
    as_of: datetime
    produced_at: datetime
    payload: dict[str, Any]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("as_of", "produced_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @classmethod
    def build(cls, **values: Any) -> "ImmutableDecisionFact":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        return cls(**payload, content_hash=canonical_hash(
            {key: value for key, value in payload.items() if key != "fact_id"}
        ))

    @model_validator(mode="after")
    def validate_fact(self) -> "ImmutableDecisionFact":
        expected = canonical_hash(self.model_dump(exclude={"fact_id", "content_hash"}))
        if expected != self.content_hash:
            raise ValueError("content_hash does not match immutable decision fact")
        return self


class EntryPlanVersion(V3Contract):
    entry_plan_id: UUID = Field(default_factory=uuid4)
    decision_id: UUID
    version: int = Field(ge=1)
    supersedes_entry_plan_id: UUID | None = None
    created_by_review_id: UUID | None = None
    created_by_position_review_id: UUID | None = None
    effective_from: datetime
    expected_horizon: ExpectedHorizon
    plan: dict[str, Any]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("effective_from")
    @classmethod
    def validate_effective_from(cls, value: datetime) -> datetime:
        return require_aware(value, "effective_from")

    @model_validator(mode="after")
    def validate_version(self) -> "EntryPlanVersion":
        creators = sum(
            value is not None
            for value in (self.created_by_review_id, self.created_by_position_review_id)
        )
        if self.version == 1 and (self.supersedes_entry_plan_id is not None or creators):
            raise ValueError("entry plan version 1 cannot supersede or be review-created")
        if self.version > 1 and (self.supersedes_entry_plan_id is None or creators != 1):
            raise ValueError("later entry plan version requires one superseded plan and creator")
        expected = canonical_hash(self.model_dump(exclude={"entry_plan_id", "content_hash"}))
        if expected != self.content_hash:
            raise ValueError("content_hash does not match entry plan version")
        return self

    @classmethod
    def build(cls, **values: Any) -> "EntryPlanVersion":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        return cls(**payload, content_hash=canonical_hash(
            {key: value for key, value in payload.items() if key != "entry_plan_id"}
        ))


class DecisionReadPage(V3Contract):
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None = None


class WatchlistTransitionCommand(V3Contract):
    target_state: WatchlistState
    reason: str = Field(min_length=1, max_length=1024)
    actor_id: str = Field(min_length=1, max_length=128)


class DecisionCorrectionCommand(V3Contract):
    old_values: dict[str, Any]
    new_values: dict[str, Any]
    reason: str = Field(min_length=1, max_length=1024)
    corrected_by: str = Field(min_length=1, max_length=128)
