from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware


class EvidenceType(StrEnum):
    FACT = "FACT"
    OFFICIAL_DISCLOSURE = "OFFICIAL_DISCLOSURE"
    VENDOR_DATA = "VENDOR_DATA"
    NEWS = "NEWS"
    OPINION = "OPINION"


class EvidenceRecord(V3Contract):
    evidence_id: UUID
    evidence_type: EvidenceType
    subject: dict[str, Any]
    source: str = Field(min_length=1, max_length=128)
    upstream_source: str | None = Field(default=None, max_length=256)
    event_time: datetime | None = None
    publish_time: datetime | None = None
    fetch_time: datetime
    known_at: datetime
    confidence: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)
    payload: dict[str, Any]

    @field_validator("event_time", "publish_time", "fetch_time", "known_at")
    @classmethod
    def validate_datetimes(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_known_at(self) -> "EvidenceRecord":
        if self.known_at < self.fetch_time:
            raise ValueError("known_at cannot be earlier than fetch_time")
        return self
