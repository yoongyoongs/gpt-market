from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.v3.infrastructure.db.base import Base


V3_SCHEMA = "v3"


class EvidenceSourceModel(Base):
    __tablename__ = "evidence_sources"
    __table_args__ = (
        CheckConstraint("priority > 0", name="positive_priority"),
        CheckConstraint(
            "rate_limit_per_minute IS NULL OR rate_limit_per_minute > 0",
            name="positive_rate_limit",
        ),
        CheckConstraint("reliability >= 0 AND reliability <= 1", name="reliability_range"),
        UniqueConstraint("code", name="uq_evidence_sources_code"),
        {"schema": V3_SCHEMA},
    )

    evidence_source_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    upstream_source: Mapped[str | None] = mapped_column(String(128))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False, server_default="v1")
    reliability: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, server_default="0.5000")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RawDocumentModel(Base):
    __tablename__ = "raw_documents"
    __table_args__ = (
        CheckConstraint("known_at >= fetch_time", name="known_after_fetch"),
        CheckConstraint("payload_size >= 0", name="nonnegative_payload_size"),
        UniqueConstraint(
            "evidence_source_id", "document_key", "content_hash",
            name="uq_raw_documents_source_document_content",
        ),
        Index("ix_raw_documents_source_document", "evidence_source_id", "document_key", "fetch_time"),
        Index("ix_raw_documents_content_hash", "content_hash"),
        {"schema": V3_SCHEMA},
    )

    raw_document_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    evidence_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_sources.evidence_source_id"), nullable=False
    )
    document_key: Mapped[str] = mapped_column(String(256), nullable=False)
    raw_reference: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_reference: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    payload_text: Mapped[str | None] = mapped_column(Text)
    payload_size: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    encoding: Mapped[str | None] = mapped_column(String(32))
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    untrusted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceRecordModel(Base):
    __tablename__ = "evidence_records"
    __table_args__ = (
        CheckConstraint("known_at >= fetch_time", name="known_after_fetch"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("relevance >= 0 AND relevance <= 1", name="relevance_range"),
        CheckConstraint(
            "source_type IN ('OFFICIAL','VENDOR','NEWS','OPINION')",
            name="valid_source_type",
        ),
        CheckConstraint("source_priority > 0", name="positive_source_priority"),
        CheckConstraint(
            "decay_model IN ('NONE','LINEAR','EXPONENTIAL','FIXED_EXPIRY')",
            name="valid_decay_model",
        ),
        CheckConstraint("decay_rate IS NULL OR decay_rate >= 0", name="nonnegative_decay_rate"),
        CheckConstraint(
            "availability IN ('AVAILABLE','EXPIRED','RETRACTED','SUPERSEDED')",
            name="valid_availability",
        ),
        UniqueConstraint(
            "raw_document_id", "parser_version", "content_hash",
            name="uq_evidence_records_raw_parser_content",
        ),
        Index("ix_evidence_records_subject", "subject_type", "subject_id", "known_at"),
        Index("ix_evidence_records_claim", "subject_type", "subject_id", "claim_key", "known_at"),
        Index(
            "ix_evidence_records_retrieval",
            "subject_type", "subject_id", "availability", "expire_at", "known_at",
        ),
        {"schema": V3_SCHEMA},
    )

    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.raw_documents.raw_document_id")
    )
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default="VENDOR")
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_key: Mapped[str] = mapped_column(String(256), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    upstream_source: Mapped[str | None] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    relevance: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decay_model: Mapped[str] = mapped_column(String(32), nullable=False, server_default="NONE")
    decay_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 8))
    availability: Mapped[str] = mapped_column(String(32), nullable=False, server_default="AVAILABLE")
    untrusted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    conflict_state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="NONE")
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_records.evidence_id")
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceFetchRunModel(Base):
    __tablename__ = "evidence_fetch_runs"
    __table_args__ = (
        CheckConstraint("expected_count >= 0", name="nonnegative_expected"),
        CheckConstraint("fetched_count >= 0", name="nonnegative_fetched"),
        CheckConstraint("raw_inserted_count >= 0", name="nonnegative_inserted"),
        CheckConstraint("duplicate_count >= 0", name="nonnegative_duplicates"),
        CheckConstraint("parsed_count >= 0", name="nonnegative_parsed"),
        CheckConstraint("evidence_count >= 0", name="nonnegative_evidence"),
        CheckConstraint("failed_count >= 0", name="nonnegative_failed"),
        CheckConstraint(
            "status IN ('RUNNING','PARTIAL','COMPLETED','FAILED')",
            name="valid_status",
        ),
        CheckConstraint("completed_at IS NULL OR completed_at >= started_at", name="valid_completion"),
        Index("ix_evidence_fetch_runs_source_started", "evidence_source_id", "started_at"),
        {"schema": V3_SCHEMA},
    )

    fetch_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    evidence_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_sources.evidence_source_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_inserted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parsed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RawDocumentParseAttemptModel(Base):
    __tablename__ = "raw_document_parse_attempts"
    __table_args__ = (
        CheckConstraint("status IN ('SUCCESS','FAILED','SKIPPED')", name="valid_status"),
        CheckConstraint("output_count >= 0", name="nonnegative_output"),
        CheckConstraint("completed_at >= started_at", name="valid_completion"),
        UniqueConstraint(
            "raw_document_id", "parser_code", "parser_version",
            name="uq_raw_document_parse_attempts_document_parser",
        ),
        {"schema": V3_SCHEMA},
    )

    parse_attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    raw_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.raw_documents.raw_document_id"), nullable=False
    )
    parser_code: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    output_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceEntityLinkModel(Base):
    __tablename__ = "evidence_entity_links"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("status IN ('CONFIRMED','CANDIDATE','REJECTED')", name="valid_status"),
        UniqueConstraint(
            "evidence_id", "entity_type", "entity_id",
            name="uq_evidence_entity_links_evidence_entity",
        ),
        Index("ix_evidence_entity_links_entity", "entity_type", "entity_id", "status"),
        {"schema": V3_SCHEMA},
    )

    entity_link_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_records.evidence_id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    match_basis: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceRelationModel(Base):
    __tablename__ = "evidence_relations"
    __table_args__ = (
        CheckConstraint("from_evidence_id <> to_evidence_id", name="distinct_records"),
        CheckConstraint(
            "similarity IS NULL OR (similarity >= 0 AND similarity <= 1)",
            name="similarity_range",
        ),
        CheckConstraint(
            "relation_type IN ('EXACT_DUPLICATE','NEAR_DUPLICATE','SUPERSEDES','CORRECTS','SUPPORTS')",
            name="valid_type",
        ),
        UniqueConstraint(
            "from_evidence_id", "to_evidence_id", "relation_type",
            name="uq_evidence_relations_pair_type",
        ),
        {"schema": V3_SCHEMA},
    )

    relation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    from_evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_records.evidence_id"), nullable=False
    )
    to_evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_records.evidence_id"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    similarity: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    reason: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceConflictModel(Base):
    __tablename__ = "evidence_conflicts"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','RESOLVED','ACKNOWLEDGED')", name="valid_status"),
        UniqueConstraint(
            "subject_type", "subject_id", "claim_key", "content_hash",
            name="uq_evidence_conflicts_claim_content",
        ),
        Index("ix_evidence_conflicts_subject", "subject_type", "subject_id", "status"),
        {"schema": V3_SCHEMA},
    )

    conflict_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claim_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_records.evidence_id")
    )
    resolution: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceConflictMemberModel(Base):
    __tablename__ = "evidence_conflict_members"
    __table_args__ = (
        CheckConstraint("source_priority > 0", name="positive_priority"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        {"schema": V3_SCHEMA},
    )

    conflict_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_conflicts.conflict_id"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_records.evidence_id"), primary_key=True
    )
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TaskProfileModel(Base):
    __tablename__ = "task_profiles"
    __table_args__ = (
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint("expected_group_count > 0", name="positive_expected_groups"),
        CheckConstraint("grace_seconds >= 0", name="nonnegative_grace"),
        UniqueConstraint("profile_code", "version", name="uq_task_profiles_code_version"),
        {"schema": V3_SCHEMA},
    )

    task_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    profile_code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schedule: Mapped[str | None] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_calendar: Mapped[str] = mapped_column(String(128), nullable=False, server_default="UNKNOWN")
    context_level: Mapped[str] = mapped_column(String(16), nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_group_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExpectedRunModel(Base):
    __tablename__ = "expected_runs"
    __table_args__ = (
        CheckConstraint("window_end >= scheduled_for", name="valid_window"),
        UniqueConstraint("task_profile_id", "scheduled_for", name="uq_expected_runs_profile_schedule"),
        {"schema": V3_SCHEMA},
    )

    expected_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.task_profiles.task_profile_id"), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="EXPECTED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TaskRunModel(Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        CheckConstraint("expected_group_count > 0", name="positive_expected_groups"),
        CheckConstraint("successful_group_count >= 0", name="nonnegative_successful_groups"),
        CheckConstraint("failed_group_count >= 0", name="nonnegative_failed_groups"),
        CheckConstraint("pending_group_count >= 0", name="nonnegative_pending_groups"),
        CheckConstraint(
            "expected_group_count = successful_group_count + failed_group_count + pending_group_count",
            name="group_count_total",
        ),
        CheckConstraint(
            "status IN ('PENDING_IMPORT','PARTIAL_COMPLETED','COMPLETED','MISSED','CANCELLED')",
            name="valid_status",
        ),
        {"schema": V3_SCHEMA},
    )

    task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    expected_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.expected_runs.expected_run_id"), unique=True
    )
    task_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.task_profiles.task_profile_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="PENDING_IMPORT")
    expected_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    successful_group_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_group_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pending_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    context_pack_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    context_pack_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AgentTaskModel(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_agent_tasks_content_hash"), {"schema": V3_SCHEMA})

    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.task_runs.task_run_id"), nullable=False)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    task_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    context_pack_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    context_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_result_type: Mapped[str] = mapped_column(String(64), nullable=False)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AIResultEnvelopeModel(Base):
    __tablename__ = "ai_result_envelopes"
    __table_args__ = (
        CheckConstraint("known_at >= produced_at", name="known_after_produced"),
        UniqueConstraint("content_hash", name="uq_ai_result_envelopes_content_hash"),
        {"schema": V3_SCHEMA},
    )

    result_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.agent_tasks.task_id"), nullable=False)
    task_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.task_runs.task_run_id"), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    result_type: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(128))
    context_pack_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    context_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    produced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuditEventModel(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_object", "object_type", "object_id", "event_time"), {"schema": V3_SCHEMA})

    audit_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128))
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SecurityModel(Base):
    __tablename__ = "securities"
    __table_args__ = (UniqueConstraint("market", "code", name="uq_securities_market_code"), {"schema": V3_SCHEMA})

    security_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(6), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    security_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default="A_SHARE")
    list_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delist_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UniverseSourceModel(Base):
    __tablename__ = "universe_sources"
    __table_args__ = (UniqueConstraint("code", name="uq_universe_sources_code"), {"schema": V3_SCHEMA})

    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    capability_version: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UniverseSnapshotModel(Base):
    __tablename__ = "universe_snapshots"
    __table_args__ = (
        CheckConstraint("known_at >= fetch_time", name="known_after_fetch"),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_range"),
        CheckConstraint("status IN ('PRIMARY','SECONDARY','LKG')", name="valid_status"),
        CheckConstraint("status <> 'LKG' OR stale", name="lkg_requires_stale"),
        UniqueConstraint("content_hash", name="uq_universe_snapshots_content_hash"),
        Index("ix_universe_snapshots_as_of", "as_of"),
        {"schema": V3_SCHEMA},
    )

    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.universe_sources.source_id"), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    coverage: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.universe_snapshots.snapshot_id")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UniverseMemberModel(Base):
    __tablename__ = "universe_members"
    __table_args__ = (
        Index("ix_universe_members_security_snapshot", "security_id", "snapshot_id"),
        {"schema": V3_SCHEMA},
    )

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.universe_snapshots.snapshot_id"), primary_key=True
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.securities.security_id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    trading_status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False)
    suspended: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_new_listing: Mapped[bool] = mapped_column(Boolean, nullable=False)
    delisting_risk: Mapped[bool] = mapped_column(Boolean, nullable=False)
    raw_reference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UniverseDiffModel(Base):
    __tablename__ = "universe_diffs"
    __table_args__ = (
        CheckConstraint("change_type IN ('ADDED','REMOVED','CHANGED')", name="valid_change_type"),
        Index("ix_universe_diffs_snapshot", "snapshot_id", "change_type"),
        {"schema": V3_SCHEMA},
    )

    diff_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.universe_snapshots.snapshot_id"), nullable=False
    )
    previous_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.universe_snapshots.snapshot_id")
    )
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    before_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MarketDataIngestionRunModel(Base):
    __tablename__ = "market_data_ingestion_runs"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','RUNNING','PARTIAL','COMPLETED','FAILED')", name="valid_status"),
        CheckConstraint("expected_count >= 0", name="nonnegative_expected"),
        CheckConstraint("processed_count >= 0", name="nonnegative_processed"),
        CheckConstraint("successful_count >= 0", name="nonnegative_successful"),
        CheckConstraint("failed_count >= 0", name="nonnegative_failed"),
        CheckConstraint("processed_count = successful_count + failed_count", name="processed_count_total"),
        CheckConstraint("processed_count <= expected_count", name="processed_not_over_expected"),
        {"schema": V3_SCHEMA},
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    universe_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.universe_snapshots.snapshot_id", name="fk_ingestion_runs_universe_snapshot")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    successful_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdjustmentFactorRevisionModel(Base):
    __tablename__ = "adjustment_factor_revisions"
    __table_args__ = (
        CheckConstraint("known_at >= fetch_time", name="known_after_fetch"),
        UniqueConstraint("content_hash", name="uq_adjustment_factor_revisions_content_hash"),
        Index("ix_adjustment_factor_revisions_security_known", "security_id", "known_at"),
        {"schema": V3_SCHEMA},
    )

    factor_revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    upstream_source: Mapped[str] = mapped_column(String(128), nullable=False)
    derivation_method: Mapped[str] = mapped_column(String(64), nullable=False)
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            f"{V3_SCHEMA}.adjustment_factor_revisions.factor_revision_id",
            name="fk_factor_revisions_supersedes",
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdjustmentFactorModel(Base):
    __tablename__ = "adjustment_factors"
    __table_args__ = (
        CheckConstraint("factor > 0", name="positive_factor"),
        {"schema": V3_SCHEMA},
    )

    factor_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            f"{V3_SCHEMA}.adjustment_factor_revisions.factor_revision_id",
            name="fk_adjustment_factors_revision",
        ),
        primary_key=True,
    )
    trading_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    factor: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BarSeriesRevisionModel(Base):
    __tablename__ = "bar_series_revisions"
    __table_args__ = (
        CheckConstraint("period IN ('DAY','WEEK','MONTH')", name="valid_period"),
        CheckConstraint("adjust_type IN ('RAW','QFQ','HFQ')", name="valid_adjust_type"),
        CheckConstraint("point_in_time_precision IN ('FULL','LIMITED')", name="valid_precision"),
        CheckConstraint("adjust_type <> 'RAW' OR raw_bar_available", name="raw_requires_available"),
        CheckConstraint(
            "point_in_time_precision <> 'LIMITED' OR precision_reason IS NOT NULL",
            name="limited_requires_reason",
        ),
        UniqueConstraint("content_hash", name="uq_bar_series_revisions_content_hash"),
        Index("ix_bar_series_revisions_security_period", "security_id", "period", "adjust_type", "known_at"),
        {"schema": V3_SCHEMA},
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    adjust_type: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    upstream_source: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_bar_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    factor_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            f"{V3_SCHEMA}.adjustment_factor_revisions.factor_revision_id",
            name="fk_bar_revisions_factor_revision",
        )
    )
    point_in_time_precision: Mapped[str] = mapped_column(String(16), nullable=False)
    precision_reason: Mapped[str | None] = mapped_column(String(512))
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.bar_series_revisions.revision_id", name="fk_bar_revisions_supersedes")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PUBLISHED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MarketBarModel(Base):
    __tablename__ = "market_bars"
    __table_args__ = (
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="positive_ohlc"),
        CheckConstraint("high >= open AND high >= close AND high >= low", name="valid_high"),
        CheckConstraint("low <= open AND low <= close AND low <= high", name="valid_low"),
        CheckConstraint("volume >= 0", name="nonnegative_volume"),
        CheckConstraint("amount >= 0", name="nonnegative_amount"),
        CheckConstraint("NOT provisional", name="published_not_provisional"),
        {"schema": V3_SCHEMA},
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.bar_series_revisions.revision_id"), primary_key=True
    )
    bar_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    provisional: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FeatureRunModel(Base):
    __tablename__ = "feature_runs"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','PUBLISHED','FAILED')", name="valid_status"),
        CheckConstraint("expected_count >= 0 AND successful_count >= 0 AND failed_count >= 0", name="nonnegative_counts"),
        CheckConstraint("successful_count + failed_count <= expected_count", name="valid_counts"),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_range"),
        UniqueConstraint("content_hash", name="uq_feature_runs_content_hash"),
        Index("ix_feature_runs_published_as_of", "status", "as_of"),
        {"schema": V3_SCHEMA},
    )

    feature_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    universe_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.universe_snapshots.snapshot_id"), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    successful_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    coverage: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    bar_revision_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SecurityFeatureModel(Base):
    __tablename__ = "security_features"
    __table_args__ = (
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_range"),
        UniqueConstraint("content_hash", name="uq_security_features_content_hash"),
        Index("ix_security_features_run_return20", "feature_run_id", "return_20d", "security_id"),
        Index("ix_security_features_run_position60", "feature_run_id", "position_60d", "security_id"),
        Index("ix_security_features_run_amount", "feature_run_id", "amount", "security_id"),
        {"schema": V3_SCHEMA},
    )

    feature_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.feature_runs.feature_run_id"), primary_key=True)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), primary_key=True)
    series_revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.bar_series_revisions.revision_id"), nullable=False)
    factor_revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.adjustment_factor_revisions.factor_revision_id"))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    return_3d: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    return_5d: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    return_10d: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    return_20d: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    return_60d: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    return_120d: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    return_250d: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    position_60d: Mapped[Decimal | None] = mapped_column(Numeric(12, 10))
    position_120d: Mapped[Decimal | None] = mapped_column(Numeric(12, 10))
    position_250d: Mapped[Decimal | None] = mapped_column(Numeric(12, 10))
    ma5: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    ma10: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    ma20: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    ma60: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    ma20_slope: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    ma60_slope: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    atr14: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    atr_pct: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    volatility20: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    distance_60d_high: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    distance_60d_low: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    breakout_20d: Mapped[bool | None] = mapped_column(Boolean)
    pullback_20d: Mapped[bool | None] = mapped_column(Boolean)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    volume_ratio_5d: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    volume_expansion: Mapped[bool | None] = mapped_column(Boolean)
    relative_index_strength: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    relative_industry_strength: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    coverage: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False)
    missing_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_errors: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    quality: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MarketRegimeSnapshotModel(Base):
    __tablename__ = "market_regime_snapshots"
    __table_args__ = (
        CheckConstraint("known_at >= as_of", name="known_after_as_of"),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        UniqueConstraint("feature_run_id", name="uq_market_regime_feature_run"),
        UniqueConstraint("content_hash", name="uq_market_regime_content_hash"),
        Index("ix_market_regime_as_of", "as_of"),
        {"schema": V3_SCHEMA},
    )

    regime_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    feature_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.feature_runs.feature_run_id"), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    index_states: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    breadth: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    turnover: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    limit_structure: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    size_style: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    growth_value_style: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    industry_rotation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    risk_appetite_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    domestic_risk_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    global_risk_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    coverage: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CorporateActionModel(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (
        CheckConstraint("known_at >= fetch_time", name="known_after_fetch"),
        UniqueConstraint("content_hash", name="uq_corporate_actions_content_hash"),
        Index("ix_corporate_actions_security_effective", "security_id", "effective_time"),
        {"schema": V3_SCHEMA},
    )

    corporate_action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    announcement_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    record_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.evidence_records.evidence_id"))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.corporate_actions.corporate_action_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
