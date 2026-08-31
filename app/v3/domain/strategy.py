from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


class StrategyStatus(StrEnum):
    DRAFT = "DRAFT"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"


class ReleaseMode(StrEnum):
    V2 = "V2"
    SHADOW = "SHADOW"
    AB = "AB"
    V3 = "V3"


class ActorType(StrEnum):
    AI = "AI"
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


class ExperimentType(StrEnum):
    SHADOW = "SHADOW"
    AB = "AB"


class StrategyVersionCreate(V3Contract):
    strategy_version_id: UUID = Field(default_factory=uuid4)
    strategy_code: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    supersedes_strategy_version_id: UUID | None = None
    configuration: dict[str, Any]
    rationale: str = Field(min_length=1)
    created_by: str = Field(min_length=1, max_length=128)
    effective_from: datetime | None = None

    @field_validator("effective_from")
    @classmethod
    def validate_effective_from(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value, "effective_from")

    @model_validator(mode="after")
    def validate_version_chain(self) -> "StrategyVersionCreate":
        if self.version == 1 and self.supersedes_strategy_version_id is not None:
            raise ValueError("strategy version 1 cannot supersede another version")
        if self.version > 1 and self.supersedes_strategy_version_id is None:
            raise ValueError("later strategy version requires a predecessor")
        return self


class StrategyProposalCreate(V3Contract):
    proposal_id: UUID = Field(default_factory=uuid4)
    proposed_strategy_version_id: UUID
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128)
    source_result_id: UUID | None = None
    hypothesis: str = Field(min_length=1)
    expected_improvements: dict[str, Any]
    risks: tuple[str, ...] = ()
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return require_aware(value, "created_at")


class GuardrailVersionCreate(V3Contract):
    guardrail_version_id: UUID = Field(default_factory=uuid4)
    guardrail_code: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    supersedes_guardrail_version_id: UUID | None = None
    max_error_rate: float = Field(ge=0, le=1)
    max_p95_ms: float = Field(gt=0)
    min_shadow_sample_count: int = Field(ge=1)
    max_divergence_rate: float = Field(ge=0, le=1)
    max_capacity_utilization: float = Field(gt=0, le=1)
    rollback_on_provider_failure: bool = True
    created_by: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_version_chain(self) -> "GuardrailVersionCreate":
        if self.version == 1 and self.supersedes_guardrail_version_id is not None:
            raise ValueError("guardrail version 1 cannot supersede another version")
        if self.version > 1 and self.supersedes_guardrail_version_id is None:
            raise ValueError("later guardrail version requires a predecessor")
        return self


class StrategyExperimentCreate(V3Contract):
    experiment_id: UUID = Field(default_factory=uuid4)
    experiment_type: ExperimentType
    control_strategy_version_id: UUID | None = None
    treatment_strategy_version_id: UUID
    guardrail_version_id: UUID
    allocation_percent: int = Field(default=0, ge=0, le=100)
    starts_at: datetime
    ends_at: datetime | None = None
    created_by: str = Field(min_length=1, max_length=128)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def validate_times(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_experiment(self) -> "StrategyExperimentCreate":
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("experiment end must be later than start")
        if self.experiment_type is ExperimentType.SHADOW and self.allocation_percent != 0:
            raise ValueError("shadow experiment cannot route user traffic")
        if self.experiment_type is ExperimentType.AB:
            if self.control_strategy_version_id is None:
                raise ValueError("A/B experiment requires a control strategy")
            if not 1 <= self.allocation_percent <= 99:
                raise ValueError("A/B allocation must be between 1 and 99")
        return self


class ShadowObservationCreate(V3Contract):
    shadow_observation_id: UUID = Field(default_factory=uuid4)
    experiment_id: UUID
    subject_key: str = Field(min_length=1, max_length=256)
    observed_at: datetime
    control_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    treatment_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_payload: dict[str, Any]
    treatment_payload: dict[str, Any]
    materially_divergent: bool
    divergence_reason: str | None = None
    latency_ms: float = Field(ge=0)
    error: str | None = None

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return require_aware(value, "observed_at")


class CapacityEvaluationCreate(V3Contract):
    capacity_evaluation_id: UUID = Field(default_factory=uuid4)
    strategy_version_id: UUID
    guardrail_version_id: UUID
    evaluated_at: datetime
    capacity_utilization: float = Field(ge=0)
    provider_failures: int = Field(ge=0)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return require_aware(value, "evaluated_at")


class StrategyActivationCommand(V3Contract):
    proposal_id: UUID
    strategy_version_id: UUID
    guardrail_version_id: UUID
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128)
    approval_reason: str = Field(min_length=1)
    target_mode: ReleaseMode = ReleaseMode.V3
    expected_row_version: int = Field(ge=0)

    @model_validator(mode="after")
    def human_only(self) -> "StrategyActivationCommand":
        if self.actor_type is not ActorType.HUMAN:
            raise ValueError("only a human can activate a strategy")
        if self.target_mode not in {ReleaseMode.SHADOW, ReleaseMode.AB, ReleaseMode.V3}:
            raise ValueError("activation target must be SHADOW, AB or V3")
        return self


class StrategyRollbackCommand(V3Contract):
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1)
    expected_row_version: int = Field(ge=0)
    target_mode: ReleaseMode = ReleaseMode.V2

    @model_validator(mode="after")
    def human_or_system_only(self) -> "StrategyRollbackCommand":
        if self.actor_type is ActorType.AI:
            raise ValueError("AI cannot roll back or activate production")
        if self.target_mode is not ReleaseMode.V2:
            raise ValueError("fast rollback target must be V2")
        return self


class ExperimentEventCommand(V3Contract):
    event_type: str = Field(pattern=r"^(STARTED|PAUSED|RESUMED|COMPLETED|STOPPED)$")
    actor_type: ActorType
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def no_ai_control(self) -> "ExperimentEventCommand":
        if self.actor_type is ActorType.AI:
            raise ValueError("AI cannot start, pause or stop an experiment")
        return self


class OperationalHealthEventCreate(V3Contract):
    health_event_id: UUID = Field(default_factory=uuid4)
    environment: str = Field(default="production", min_length=1, max_length=32)
    component: str = Field(min_length=1, max_length=128)
    capability: str = Field(min_length=1, max_length=128)
    status: str = Field(pattern=r"^(HEALTHY|DEGRADED|FAILED|UNKNOWN)$")
    latency_ms: float | None = Field(default=None, ge=0)
    error_type: str | None = Field(default=None, max_length=128)
    circuit_state: str = Field(min_length=1, max_length=32)
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def validate_health_time(cls, value: datetime) -> datetime:
        return require_aware(value, "observed_at")


def content_hash(value: V3Contract | dict[str, Any]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, V3Contract) else value
    return canonical_hash(payload)
