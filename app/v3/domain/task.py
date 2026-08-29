from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskRunStatus(StrEnum):
    PENDING_IMPORT = "PENDING_IMPORT"
    PARTIAL_COMPLETED = "PARTIAL_COMPLETED"
    COMPLETED = "COMPLETED"
    MISSED = "MISSED"
    CANCELLED = "CANCELLED"


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
