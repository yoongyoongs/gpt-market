from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.context import ContextLevel
from app.v3.domain.hashing import canonical_hash


class ExpectedRunStatus(StrEnum):
    EXPECTED = "EXPECTED"
    CANCELLED = "CANCELLED"


class TaskRunStatus(StrEnum):
    PENDING_IMPORT = "PENDING_IMPORT"
    PARTIAL_COMPLETED = "PARTIAL_COMPLETED"
    COMPLETED = "COMPLETED"
    MISSED = "MISSED"
    CANCELLED = "CANCELLED"


class TaskProfile(V3Contract):
    task_profile_id: UUID = Field(default_factory=uuid4)
    profile_code: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    schedule: str | None = Field(default=None, max_length=128)
    timezone: str = Field(min_length=1, max_length=64)
    trading_calendar_source: str = Field(min_length=1, max_length=128)
    trading_calendar_version: str = Field(min_length=1, max_length=64)
    context_level: ContextLevel
    comparison_first: bool
    candidate_limit: int | None = Field(default=None, ge=20, le=100)
    topk_limit: int | None = Field(default=None, ge=1, le=100)
    topk_context_level: ContextLevel | None = None
    output_schema: dict[str, Any] = Field(min_length=1)
    expected_group_count: int = Field(default=1, ge=1)
    grace_seconds: int = Field(default=0, ge=0)
    strategy_version: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_profile(self) -> "TaskProfile":
        comparison_fields = (
            self.candidate_limit,
            self.topk_limit,
            self.topk_context_level,
        )
        if self.comparison_first:
            if any(value is None for value in comparison_fields):
                raise ValueError("comparison-first profile requires candidate/topk settings")
            if self.topk_limit > self.candidate_limit:  # type: ignore[operator]
                raise ValueError("topk_limit cannot exceed candidate_limit")
            if self.topk_context_level not in {ContextLevel.NORMAL, ContextLevel.DEEP}:
                raise ValueError("topk context level must be NORMAL or DEEP")
        elif any(value is not None for value in comparison_fields):
            raise ValueError("non-comparison profile cannot define candidate/topk settings")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match task profile")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(self.model_dump(exclude={"task_profile_id", "content_hash"}))

    @classmethod
    def build(cls, **values: Any) -> "TaskProfile":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        content_hash = canonical_hash(
            {key: value for key, value in payload.items() if key != "task_profile_id"}
        )
        return cls(**payload, content_hash=content_hash)


class ExpectedRun(V3Contract):
    expected_run_id: UUID = Field(default_factory=uuid4)
    task_profile_id: UUID
    task_profile_version: int = Field(ge=1)
    scheduled_for: datetime
    window_end: datetime
    status: ExpectedRunStatus = ExpectedRunStatus.EXPECTED
    known_at: datetime
    row_version: int = Field(default=1, ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("scheduled_for", "window_end", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_expected_run(self) -> "ExpectedRun":
        if self.window_end < self.scheduled_for:
            raise ValueError("window_end cannot be earlier than scheduled_for")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match expected run")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(
            self.model_dump(
                exclude={"expected_run_id", "known_at", "row_version", "content_hash"}
            )
        )

    @classmethod
    def build(cls, **values: Any) -> "ExpectedRun":
        payload = cls.model_construct(**values, content_hash="0" * 64).model_dump(
            exclude={"content_hash"}
        )
        content_hash = canonical_hash(
            {
                key: value
                for key, value in payload.items()
                if key not in {"expected_run_id", "known_at", "row_version"}
            }
        )
        return cls(**payload, content_hash=content_hash)


class TaskGroupCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected: int = Field(ge=1)
    successful: int = Field(ge=0)
    failed: int = Field(ge=0)
    pending: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> "TaskGroupCounts":
        if self.successful + self.failed + self.pending != self.expected:
            raise ValueError("expected must equal successful + failed + pending")
        return self


class TaskRun(V3Contract):
    task_run_id: UUID = Field(default_factory=uuid4)
    expected_run_id: UUID | None = None
    task_profile_id: UUID
    task_profile_version: int = Field(ge=1)
    status: TaskRunStatus = TaskRunStatus.PENDING_IMPORT
    counts: TaskGroupCounts
    context_pack_id: UUID | None = None
    context_pack_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at: datetime | None = None
    completed_at: datetime | None = None
    row_version: int = Field(default=1, ge=1)

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_times(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_run(self) -> "TaskRun":
        if (self.context_pack_id is None) != (self.context_pack_hash is None):
            raise ValueError("context pack id and hash must be provided together")
        if self.completed_at is not None and self.started_at is None:
            raise ValueError("completed task run requires started_at")
        if self.started_at and self.completed_at and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        derived = derive_task_run_status(
            self.counts,
            cancelled=self.status is TaskRunStatus.CANCELLED,
            grace_period_expired=self.status is TaskRunStatus.MISSED,
        )
        if derived is not self.status:
            raise ValueError("task run status does not match group counts")
        return self


class TaskRunReadPage(V3Contract):
    items: tuple[TaskRun, ...]
    next_cursor: str | None = None


def derive_task_run_status(
    counts: TaskGroupCounts, *, cancelled: bool = False, grace_period_expired: bool = False
) -> TaskRunStatus:
    if cancelled:
        return TaskRunStatus.CANCELLED
    if counts.successful == counts.expected:
        return TaskRunStatus.COMPLETED
    if counts.successful > 0:
        return TaskRunStatus.PARTIAL_COMPLETED
    if grace_period_expired:
        return TaskRunStatus.MISSED
    return TaskRunStatus.PENDING_IMPORT
