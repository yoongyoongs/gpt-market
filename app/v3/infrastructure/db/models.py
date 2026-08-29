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
    __table_args__ = (UniqueConstraint("code", name="uq_evidence_sources_code"), {"schema": V3_SCHEMA})

    evidence_source_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RawDocumentModel(Base):
    __tablename__ = "raw_documents"
    __table_args__ = (
        CheckConstraint("known_at >= fetch_time", name="known_after_fetch"),
        UniqueConstraint("content_hash", name="uq_raw_documents_content_hash"),
        {"schema": V3_SCHEMA},
    )

    raw_document_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    evidence_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_sources.evidence_source_id"), nullable=False
    )
    raw_reference: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
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
        UniqueConstraint("content_hash", name="uq_evidence_records_content_hash"),
        Index("ix_evidence_records_subject", "subject_type", "subject_id", "known_at"),
        {"schema": V3_SCHEMA},
    )

    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.raw_documents.raw_document_id")
    )
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    upstream_source: Mapped[str | None] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    relevance: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conflict_state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="NONE")
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
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
