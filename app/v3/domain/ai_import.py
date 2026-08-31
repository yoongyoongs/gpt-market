from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.agent import AIResultEnvelope, AgentIdentity
from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


class AIResultType(StrEnum):
    MARKET_REVIEW = "MarketReview"
    CANDIDATE_COMPARISON = "CandidateComparisonResult"
    DECISION = "DecisionResult"
    REVIEW = "ReviewResult"
    WATCHLIST_PROPOSAL = "WatchlistProposal"
    ENTRY_PLAN = "EntryPlanResult"
    POSITION_REVIEW = "PositionReviewResult"


class ImportStatus(StrEnum):
    PREVIEWED = "PREVIEWED"
    CONFIRMED = "CONFIRMED"
    PARTIAL_COMPLETED = "PARTIAL_COMPLETED"
    FAILED = "FAILED"


class GroupCommitStatus(StrEnum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"


class AIResultAtomicGroup(V3Contract):
    group_id: str = Field(min_length=1, max_length=128)
    task_run_id: UUID
    subject: dict[str, Any] = Field(default_factory=dict)
    required: bool = True
    results: tuple[AIResultEnvelope, ...] = Field(min_length=1)
    dependencies: dict[UUID, tuple[UUID, ...]] = Field(default_factory=dict)
    group_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, **values: Any) -> "AIResultAtomicGroup":
        payload = cls.model_construct(**values, group_hash="0" * 64).model_dump(
            exclude={"group_hash"}
        )
        return cls(**payload, group_hash=canonical_hash(payload))

    @model_validator(mode="after")
    def validate_group(self) -> "AIResultAtomicGroup":
        result_ids = {item.result_id for item in self.results}
        if len(result_ids) != len(self.results):
            raise ValueError("atomic group result IDs must be unique")
        if any(item.task_run_id != self.task_run_id for item in self.results):
            raise ValueError("all atomic group results must share task_run_id")
        for result_id, dependencies in self.dependencies.items():
            if result_id not in result_ids or not set(dependencies) <= result_ids:
                raise ValueError("atomic group dependencies must remain inside the group")
            if result_id in dependencies:
                raise ValueError("result cannot depend on itself")
        forbidden = {"actual_trade", "trade_executed", "holding_confirmed"}
        for item in self.results:
            if forbidden.intersection(item.result):
                raise ValueError("AI result cannot assert unconfirmed trade or holding facts")
        if canonical_hash(self.model_dump(exclude={"group_hash"})) != self.group_hash:
            raise ValueError("group_hash does not match atomic group")
        return self


class AIResultBundle(V3Contract):
    schema_version: str = "v3.0"
    bundle_id: UUID = Field(default_factory=uuid4)
    agent: AgentIdentity
    task_run_ids: tuple[UUID, ...] = Field(min_length=1)
    produced_at: datetime
    atomic_groups: tuple[AIResultAtomicGroup, ...] = Field(min_length=1)
    bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("produced_at")
    @classmethod
    def validate_produced_at(cls, value: datetime) -> datetime:
        return require_aware(value, "produced_at")

    @classmethod
    def build(cls, **values: Any) -> "AIResultBundle":
        payload = cls.model_construct(**values, bundle_hash="0" * 64).model_dump(
            exclude={"bundle_hash"}
        )
        hash_payload = {key: value for key, value in payload.items() if key != "bundle_id"}
        return cls(**payload, bundle_hash=canonical_hash(hash_payload))

    @model_validator(mode="after")
    def validate_bundle(self) -> "AIResultBundle":
        if len(set(self.task_run_ids)) != len(self.task_run_ids):
            raise ValueError("task_run_ids must be unique")
        group_ids = [item.group_id for item in self.atomic_groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("atomic group IDs must be unique")
        if any(item.task_run_id not in self.task_run_ids for item in self.atomic_groups):
            raise ValueError("atomic group task run must be declared by the bundle")
        expected = canonical_hash(
            self.model_dump(exclude={"bundle_id", "bundle_hash"})
        )
        if expected != self.bundle_hash:
            raise ValueError("bundle_hash does not match bundle")
        return self

    @classmethod
    def from_single(cls, envelope: AIResultEnvelope) -> "AIResultBundle":
        group = AIResultAtomicGroup.build(
            group_id=f"single-{envelope.result_id}",
            task_run_id=envelope.task_run_id,
            subject=envelope.result.get("subject", {}),
            results=(envelope,),
            dependencies={},
        )
        return cls.build(
            agent=envelope.agent,
            task_run_ids=(envelope.task_run_id,),
            produced_at=envelope.produced_at,
            atomic_groups=(group,),
        )


class ImportGroupPreview(V3Contract):
    group_id: str
    task_run_id: UUID
    valid: bool
    result_ids: tuple[UUID, ...]
    creates: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class AIResultImportPreview(V3Contract):
    import_id: UUID = Field(default_factory=uuid4)
    preview_revision: int = Field(default=1, ge=1)
    bundle: AIResultBundle
    groups: tuple[ImportGroupPreview, ...]
    status: ImportStatus = ImportStatus.PREVIEWED
    created_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return require_aware(value, "created_at")


class AIResultConfirmCommand(V3Contract):
    preview_revision: int = Field(ge=1)
    bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=16, max_length=128)
    confirmed_by: str = Field(min_length=1, max_length=128)


class ConfirmedGroup(V3Contract):
    group_id: str
    status: GroupCommitStatus
    result_ids: tuple[UUID, ...] = ()
    created_object_ids: tuple[UUID, ...] = ()
    error: str | None = None
    retryable: bool = False


class AIResultConfirmResult(V3Contract):
    import_id: UUID
    status: ImportStatus
    successful_groups: tuple[ConfirmedGroup, ...]
    failed_groups: tuple[ConfirmedGroup, ...]
    task_run_statuses: dict[UUID, str]

