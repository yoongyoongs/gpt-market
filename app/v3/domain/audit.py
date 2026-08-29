from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.v3.contracts.base import V3Contract, require_aware


class AuditEvent(V3Contract):
    audit_id: UUID
    actor_type: str = Field(min_length=1, max_length=32)
    actor_id: str | None = Field(default=None, max_length=128)
    action: str = Field(min_length=1, max_length=128)
    object_type: str = Field(min_length=1, max_length=64)
    object_id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)
    before_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result: str = Field(min_length=1, max_length=32)
    event_time: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_time")
    @classmethod
    def validate_event_time(cls, value: datetime) -> datetime:
        return require_aware(value, "event_time")
