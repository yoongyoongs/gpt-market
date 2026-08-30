from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.hashing import canonical_hash


class EvidenceSourceType(StrEnum):
    OFFICIAL = "OFFICIAL"
    VENDOR = "VENDOR"
    NEWS = "NEWS"
    OPINION = "OPINION"


class FetchRunStatus(StrEnum):
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ParseStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class EntityLinkStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"


class EvidenceRelationType(StrEnum):
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    NEAR_DUPLICATE = "NEAR_DUPLICATE"
    SUPERSEDES = "SUPERSEDES"
    CORRECTS = "CORRECTS"
    SUPPORTS = "SUPPORTS"


class ConflictStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class DecayModel(StrEnum):
    NONE = "NONE"
    LINEAR = "LINEAR"
    EXPONENTIAL = "EXPONENTIAL"
    FIXED_EXPIRY = "FIXED_EXPIRY"


class EvidenceAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    EXPIRED = "EXPIRED"
    RETRACTED = "RETRACTED"
    SUPERSEDED = "SUPERSEDED"


class EvidenceSource(V3Contract):
    evidence_source_id: UUID = Field(default_factory=uuid4)
    code: str = Field(min_length=1, max_length=64)
    source_type: EvidenceSourceType
    upstream_source: str | None = Field(default=None, max_length=128)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, gt=0)
    rate_limit_per_minute: int | None = Field(default=None, gt=0)
    parser_version: str = Field(default="v1", min_length=1, max_length=64)
    reliability: float = Field(default=0.5, ge=0, le=1)
    enabled: bool = True


class EvidenceFetchRun(V3Contract):
    fetch_run_id: UUID = Field(default_factory=uuid4)
    evidence_source_id: UUID
    status: FetchRunStatus = FetchRunStatus.RUNNING
    window_start: datetime | None = None
    window_end: datetime | None = None
    cursor: dict[str, Any] = Field(default_factory=dict)
    expected_count: int = Field(default=0, ge=0)
    fetched_count: int = Field(default=0, ge=0)
    raw_inserted_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    parsed_count: int = Field(default=0, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    errors: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None
    row_version: int = Field(default=1, ge=1)

    @field_validator("window_start", "window_end", "started_at", "completed_at")
    @classmethod
    def validate_times(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_run(self) -> "EvidenceFetchRun":
        if self.window_start and self.window_end and self.window_end < self.window_start:
            raise ValueError("window_end cannot be earlier than window_start")
        if self.raw_inserted_count + self.duplicate_count > self.fetched_count:
            raise ValueError("raw counts cannot exceed fetched_count")
        if self.parsed_count + self.failed_count > self.fetched_count:
            raise ValueError("parse counts cannot exceed fetched_count")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        if self.status in {FetchRunStatus.COMPLETED, FetchRunStatus.PARTIAL, FetchRunStatus.FAILED}:
            if self.completed_at is None:
                raise ValueError("terminal fetch run requires completed_at")
        elif self.completed_at is not None:
            raise ValueError("running fetch run cannot have completed_at")
        return self

    def checkpoint(
        self,
        *,
        cursor: dict[str, Any] | None,
        expected_count: int | None,
        fetched_count: int,
        raw_inserted_count: int,
        duplicate_count: int,
        parsed_count: int,
        evidence_count: int,
        failed_count: int,
        errors: dict[str, str],
    ) -> "EvidenceFetchRun":
        if self.status is not FetchRunStatus.RUNNING:
            raise ValueError("only a running fetch run can checkpoint")
        merged_errors = {**self.errors, **errors}
        return type(self).model_validate({**self.model_dump(),
            "cursor": cursor or {},
            "expected_count": max(self.expected_count, expected_count or 0),
            "fetched_count": self.fetched_count + fetched_count,
            "raw_inserted_count": self.raw_inserted_count + raw_inserted_count,
            "duplicate_count": self.duplicate_count + duplicate_count,
            "parsed_count": self.parsed_count + parsed_count,
            "evidence_count": self.evidence_count + evidence_count,
            "failed_count": self.failed_count + failed_count,
            "errors": merged_errors,
            "row_version": self.row_version + 1,
        })

    def finish(self, *, completed_at: datetime, exhausted: bool) -> "EvidenceFetchRun":
        if not exhausted:
            return self
        if self.failed_count == 0:
            status = FetchRunStatus.COMPLETED
        elif self.parsed_count > 0:
            status = FetchRunStatus.PARTIAL
        else:
            status = FetchRunStatus.FAILED
        return type(self).model_validate({**self.model_dump(),
            "status": status,
            "completed_at": completed_at,
            "row_version": self.row_version + 1,
        })

    def fail(self, *, completed_at: datetime, error: str) -> "EvidenceFetchRun":
        status = FetchRunStatus.PARTIAL if self.parsed_count > 0 else FetchRunStatus.FAILED
        return type(self).model_validate({**self.model_dump(),
            "status": status,
            "errors": {**self.errors, "provider": error},
            "completed_at": completed_at,
            "row_version": self.row_version + 1,
        })


class FetchedDocument(V3Contract):
    document_key: str = Field(min_length=1, max_length=256)
    raw_reference: str = Field(min_length=1)
    mime_type: str | None = Field(default=None, max_length=128)
    payload_text: str | None = None
    storage_path: str | None = None
    encoding: str | None = Field(default="utf-8", max_length=32)
    response_metadata: dict[str, Any] = Field(default_factory=dict)
    fetch_time: datetime
    known_at: datetime

    @field_validator("fetch_time", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_document(self) -> "FetchedDocument":
        if self.known_at < self.fetch_time:
            raise ValueError("known_at cannot be earlier than fetch_time")
        if self.payload_text is None and self.storage_path is None:
            raise ValueError("raw document requires payload_text or storage_path")
        return self


class RawDocument(V3Contract):
    raw_document_id: UUID = Field(default_factory=uuid4)
    evidence_source_id: UUID
    document_key: str
    raw_reference: str
    normalized_reference: str
    storage_path: str | None = None
    mime_type: str | None = None
    payload_text: str | None = None
    payload_size: int = Field(ge=0)
    encoding: str | None = None
    response_metadata: dict[str, Any] = Field(default_factory=dict)
    untrusted: bool = True
    fetch_time: datetime
    known_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("fetch_time", "known_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @classmethod
    def build(
        cls,
        *,
        evidence_source_id: UUID,
        fetched: FetchedDocument,
        normalized_reference: str,
    ) -> "RawDocument":
        payload_size = len((fetched.payload_text or "").encode(fetched.encoding or "utf-8"))
        content_hash = canonical_hash({
            "mime_type": fetched.mime_type,
            "payload_text": fetched.payload_text,
            "storage_path": fetched.storage_path,
        })
        return cls(
            evidence_source_id=evidence_source_id,
            document_key=fetched.document_key,
            raw_reference=fetched.raw_reference,
            normalized_reference=normalized_reference,
            storage_path=fetched.storage_path,
            mime_type=fetched.mime_type,
            payload_text=fetched.payload_text,
            payload_size=payload_size,
            encoding=fetched.encoding,
            response_metadata=fetched.response_metadata,
            fetch_time=fetched.fetch_time,
            known_at=fetched.known_at,
            content_hash=content_hash,
        )


class NormalizedEvidence(V3Contract):
    evidence_id: UUID = Field(default_factory=uuid4)
    raw_document_id: UUID
    evidence_type: EvidenceType
    source_type: EvidenceSourceType
    source_priority: int = Field(gt=0)
    subject_type: str = Field(min_length=1, max_length=32)
    subject_id: str = Field(min_length=1, max_length=64)
    claim_key: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=128)
    upstream_source: str | None = Field(default=None, max_length=256)
    payload: dict[str, Any]
    normalized_payload: dict[str, Any]
    event_time: datetime | None = None
    publish_time: datetime | None = None
    fetch_time: datetime
    known_at: datetime
    confidence: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)
    expire_at: datetime | None = None
    decay_model: DecayModel = DecayModel.NONE
    decay_rate: float | None = Field(default=None, ge=0)
    availability: EvidenceAvailability = EvidenceAvailability.AVAILABLE
    untrusted: bool = True
    conflict_state: str = "NONE"
    parser_version: str = Field(min_length=1, max_length=64)
    supersedes_evidence_id: UUID | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("event_time", "publish_time", "fetch_time", "known_at", "expire_at")
    @classmethod
    def validate_times(cls, value: datetime | None, info) -> datetime | None:
        return None if value is None else require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_evidence(self) -> "NormalizedEvidence":
        if self.known_at < self.fetch_time:
            raise ValueError("known_at cannot be earlier than fetch_time")
        if self.expire_at is not None and self.expire_at < self.known_at:
            raise ValueError("expire_at cannot be earlier than known_at")
        if self.source_type is EvidenceSourceType.OPINION and self.evidence_type is not EvidenceType.OPINION:
            raise ValueError("opinion source cannot be upgraded to factual evidence")
        if self.evidence_type is EvidenceType.OFFICIAL_DISCLOSURE and self.source_type is not EvidenceSourceType.OFFICIAL:
            raise ValueError("official disclosure requires an official source")
        if self.decay_model in {DecayModel.LINEAR, DecayModel.EXPONENTIAL} and self.decay_rate is None:
            raise ValueError("decay_rate is required for linear or exponential decay")
        if self.decay_model is DecayModel.FIXED_EXPIRY and self.expire_at is None:
            raise ValueError("expire_at is required for fixed expiry")
        expected = self.computed_content_hash()
        if self.content_hash != expected:
            raise ValueError("content_hash does not match normalized evidence")
        return self

    def computed_content_hash(self) -> str:
        payload = self.model_dump(exclude={"evidence_id", "content_hash"})
        payload["confidence"] = float(self.confidence)
        payload["relevance"] = float(self.relevance)
        if self.decay_rate is not None:
            payload["decay_rate"] = float(self.decay_rate)
        return canonical_hash(payload)

    @classmethod
    def build(cls, **values: Any) -> "NormalizedEvidence":
        payload = cls.model_construct(**values, content_hash="0" * 64)
        normalized = payload.model_dump(exclude={"content_hash"})
        return cls(**normalized, content_hash=payload.computed_content_hash())

    def effective_relevance(self, as_of: datetime) -> float:
        as_of = require_aware(as_of, "as_of")
        if as_of < self.known_at or self.availability is not EvidenceAvailability.AVAILABLE:
            return 0.0
        if self.expire_at is not None and as_of > self.expire_at:
            return 0.0
        age_days = max(0.0, (as_of - (self.event_time or self.publish_time or self.known_at)).total_seconds() / 86400)
        if self.decay_model is DecayModel.LINEAR:
            return max(0.0, self.relevance * (1 - (self.decay_rate or 0) * age_days))
        if self.decay_model is DecayModel.EXPONENTIAL:
            return self.relevance * math.exp(-(self.decay_rate or 0) * age_days)
        return self.relevance


class ParseAttempt(V3Contract):
    parse_attempt_id: UUID = Field(default_factory=uuid4)
    raw_document_id: UUID
    parser_code: str = Field(min_length=1, max_length=64)
    parser_version: str = Field(min_length=1, max_length=64)
    status: ParseStatus
    output_count: int = Field(ge=0)
    error: str | None = None
    started_at: datetime
    completed_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_attempt(self) -> "ParseAttempt":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        if self.status is ParseStatus.SUCCESS and self.error is not None:
            raise ValueError("successful parse attempt cannot contain an error")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match parse attempt")
        return self

    def computed_content_hash(self) -> str:
        return canonical_hash(self.model_dump(exclude={"parse_attempt_id", "content_hash"}))

    @classmethod
    def build(cls, **values: Any) -> "ParseAttempt":
        payload = cls.model_construct(**values, content_hash="0" * 64)
        return cls(**values, content_hash=payload.computed_content_hash())


class EntityLink(V3Contract):
    entity_link_id: UUID = Field(default_factory=uuid4)
    evidence_id: UUID
    entity_type: str = Field(min_length=1, max_length=32)
    entity_id: str = Field(min_length=1, max_length=128)
    match_basis: dict[str, Any]
    confidence: float = Field(ge=0, le=1)
    status: EntityLinkStatus
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, **values: Any) -> "EntityLink":
        payload = cls.model_construct(**values, content_hash="0" * 64)
        content_hash = canonical_hash(payload.model_dump(exclude={"entity_link_id", "content_hash"}))
        return cls(**values, content_hash=content_hash)


class EvidenceRelation(V3Contract):
    relation_id: UUID = Field(default_factory=uuid4)
    from_evidence_id: UUID
    to_evidence_id: UUID
    relation_type: EvidenceRelationType
    similarity: float | None = Field(default=None, ge=0, le=1)
    reason: str | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_relation(self) -> "EvidenceRelation":
        if self.from_evidence_id == self.to_evidence_id:
            raise ValueError("evidence relation requires distinct records")
        if self.content_hash != self.computed_content_hash():
            raise ValueError("content_hash does not match evidence relation")
        return self

    def computed_content_hash(self) -> str:
        payload = self.model_dump(exclude={"relation_id", "content_hash"})
        if self.similarity is not None:
            payload["similarity"] = float(self.similarity)
        return canonical_hash(payload)

    @classmethod
    def build(cls, **values: Any) -> "EvidenceRelation":
        payload = cls.model_construct(**values, content_hash="0" * 64)
        normalized = payload.model_dump(exclude={"content_hash"})
        return cls(**normalized, content_hash=payload.computed_content_hash())


class EvidenceConflict(V3Contract):
    conflict_id: UUID = Field(default_factory=uuid4)
    subject_type: str
    subject_id: str
    claim_key: str
    status: ConflictStatus = ConflictStatus.OPEN
    selected_evidence_id: UUID | None = None
    resolution: str | None = None
    member_ids: tuple[UUID, ...] = Field(min_length=2)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_conflict(self) -> "EvidenceConflict":
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("conflict members must be unique")
        if self.status is ConflictStatus.RESOLVED and self.selected_evidence_id is None:
            raise ValueError("resolved conflict requires selected_evidence_id")
        if self.selected_evidence_id is not None and self.selected_evidence_id not in self.member_ids:
            raise ValueError("selected evidence must be a conflict member")
        return self

    @classmethod
    def build(cls, **values: Any) -> "EvidenceConflict":
        member_ids = tuple(sorted(values["member_ids"], key=str))
        normalized = {**values, "member_ids": member_ids}
        payload = cls.model_construct(**normalized, content_hash="0" * 64)
        content_hash = canonical_hash(payload.model_dump(exclude={"conflict_id", "content_hash"}))
        return cls(**normalized, content_hash=content_hash)
