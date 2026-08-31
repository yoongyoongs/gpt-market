from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.decision import ExpectedHorizon, TimeEfficiencyState
from app.v3.domain.hashing import canonical_hash


FORBIDDEN_UNIFIED_SCORES = {
    "final_total_score", "action_total_score", "final_rank_score", "opportunity_score"
}


class ActionState(StrEnum):
    OBSERVE = "OBSERVE"
    ACTIONABLE = "ACTIONABLE"
    DEFERRED = "DEFERRED"
    INVALIDATED = "INVALIDATED"


class EntryReadiness(StrEnum):
    NOT_READY = "NOT_READY"
    WAIT_TRIGGER = "WAIT_TRIGGER"
    READY = "READY"
    CANCELLED = "CANCELLED"


class ActionCandidateCreate(V3Contract):
    action_candidate_id: UUID = Field(default_factory=uuid4)
    raw_opportunity_id: UUID
    security_id: UUID
    task_run_id: UUID
    context_pack_id: UUID
    context_pack_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_state: ActionState
    expected_horizon: ExpectedHorizon
    time_efficiency: TimeEfficiencyState
    time_efficiency_reason: str = Field(min_length=1)
    supporting_facts: dict[str, Any]
    contrary_facts: dict[str, Any]
    conditions: dict[str, Any]
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")

    @model_validator(mode="after")
    def reject_unified_score(self) -> "ActionCandidateCreate":
        keys = set(self.model_dump()) | set(self.supporting_facts) | set(self.conditions)
        if keys & FORBIDDEN_UNIFIED_SCORES:
            raise ValueError("Raw/Action layers cannot define a unified final score")
        return self


class EntryAssessmentCreate(V3Contract):
    entry_assessment_id: UUID = Field(default_factory=uuid4)
    action_candidate_id: UUID
    entry_plan_id: UUID | None = None
    readiness: EntryReadiness
    trigger_facts: dict[str, Any]
    cancel_facts: dict[str, Any]
    time_efficiency: TimeEfficiencyState
    explanation: str = Field(min_length=1)
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")

    @model_validator(mode="after")
    def reject_unified_score(self) -> "EntryAssessmentCreate":
        if (set(self.trigger_facts) | set(self.cancel_facts)) & FORBIDDEN_UNIFIED_SCORES:
            raise ValueError("Entry layer cannot define a unified final score")
        return self


def content_hash(value: V3Contract) -> str:
    return canonical_hash(value.model_dump(mode="json"))
