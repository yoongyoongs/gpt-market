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
    Identity,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
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
        CheckConstraint(
            "(comparison_first AND candidate_limit BETWEEN 20 AND 100 "
            "AND topk_limit BETWEEN 1 AND candidate_limit "
            "AND topk_context_level IN ('NORMAL','DEEP')) OR "
            "(NOT comparison_first AND candidate_limit IS NULL "
            "AND topk_limit IS NULL AND topk_context_level IS NULL)",
            name="valid_comparison_settings",
        ),
        UniqueConstraint("profile_code", "version", name="uq_task_profiles_code_version"),
        UniqueConstraint("content_hash", name="uq_task_profiles_content_hash"),
        {"schema": V3_SCHEMA},
    )

    task_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    profile_code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schedule: Mapped[str | None] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_calendar: Mapped[str] = mapped_column(String(128), nullable=False, server_default="UNKNOWN")
    trading_calendar_source: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default="UNKNOWN"
    )
    trading_calendar_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="UNKNOWN"
    )
    context_level: Mapped[str] = mapped_column(String(16), nullable=False)
    comparison_first: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    candidate_limit: Mapped[int | None] = mapped_column(Integer)
    topk_limit: Mapped[int | None] = mapped_column(Integer)
    topk_context_level: Mapped[str | None] = mapped_column(String(16))
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_group_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    strategy_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="UNKNOWN"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pre_phase6_content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExpectedRunModel(Base):
    __tablename__ = "expected_runs"
    __table_args__ = (
        CheckConstraint("window_end >= scheduled_for", name="valid_window"),
        CheckConstraint("status IN ('EXPECTED','CANCELLED')", name="valid_status"),
        UniqueConstraint("task_profile_id", "scheduled_for", name="uq_expected_runs_profile_schedule"),
        UniqueConstraint("content_hash", name="uq_expected_runs_content_hash"),
        {"schema": V3_SCHEMA},
    )

    expected_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.task_profiles.task_profile_id"), nullable=False
    )
    task_profile_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="EXPECTED")
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
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
        CheckConstraint(
            "(status = 'COMPLETED' AND successful_group_count = expected_group_count) OR "
            "(status = 'PARTIAL_COMPLETED' AND successful_group_count > 0 "
            "AND successful_group_count < expected_group_count) OR "
            "(status IN ('PENDING_IMPORT','MISSED') AND successful_group_count = 0) OR "
            "status = 'CANCELLED'",
            name="status_count_consistency",
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
    task_profile_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="PENDING_IMPORT")
    expected_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    successful_group_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_group_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pending_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    context_pack_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.context_packs.context_pack_id")
    )
    context_pack_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CandidateComparisonPackModel(Base):
    __tablename__ = "candidate_comparison_packs"
    __table_args__ = (
        CheckConstraint("known_at >= as_of", name="known_after_as_of"),
        CheckConstraint("candidate_count BETWEEN 20 AND 100", name="candidate_count_range"),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_range"),
        UniqueConstraint("content_hash", name="uq_candidate_comparison_packs_content_hash"),
        Index("ix_candidate_comparison_packs_as_of", "as_of", "known_at"),
        Index("ix_candidate_comparison_packs_feature_as_of", "feature_run_id", "as_of"),
        Index("ix_candidate_comparison_packs_recall", "recall_run_id"),
        {"schema": V3_SCHEMA},
    )

    comparison_pack_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    candidate_set_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    builder_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    field_profile_version: Mapped[str] = mapped_column(String(64), nullable=False)
    universe_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.universe_snapshots.snapshot_id"), nullable=False
    )
    feature_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.feature_runs.feature_run_id"), nullable=False
    )
    recall_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.recall_runs.recall_run_id")
    )
    regime_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.market_regime_snapshots.regime_snapshot_id")
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    missing_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trim_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CandidateComparisonMemberModel(Base):
    __tablename__ = "candidate_comparison_members"
    __table_args__ = (
        CheckConstraint("candidate_order BETWEEN 1 AND 100", name="candidate_order_range"),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_range"),
        UniqueConstraint(
            "comparison_pack_id",
            "candidate_order",
            name="uq_candidate_comparison_members_pack_order",
        ),
        Index("ix_candidate_comparison_members_security", "security_id"),
        {"schema": V3_SCHEMA},
    )

    comparison_pack_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.candidate_comparison_packs.comparison_pack_id"),
        primary_key=True,
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.securities.security_id"), primary_key=True
    )
    candidate_order: Mapped[int] = mapped_column(Integer, nullable=False)
    compact_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coverage: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False)
    missing_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class ContextPackModel(Base):
    __tablename__ = "context_packs"
    __table_args__ = (
        CheckConstraint("context_level IN ('FAST','NORMAL','DEEP')", name="valid_level"),
        CheckConstraint("subject_type IN ('SECURITY','MARKET')", name="valid_subject_type"),
        CheckConstraint("task_profile_version > 0", name="positive_profile_version"),
        CheckConstraint("known_at >= as_of", name="known_after_as_of"),
        CheckConstraint(
            "actual_tokens >= 0 AND actual_tokens <= token_budget", name="valid_token_count"
        ),
        CheckConstraint(
            "(context_level = 'FAST' AND token_budget BETWEEN 2000 AND 4000) OR "
            "(context_level = 'NORMAL' AND token_budget BETWEEN 5000 AND 8000) OR "
            "(context_level = 'DEEP' AND token_budget BETWEEN 10000 AND 14000)",
            name="valid_token_budget",
        ),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_range"),
        UniqueConstraint("content_hash", name="uq_context_packs_content_hash"),
        Index("ix_context_packs_subject_as_of", "subject_type", "subject_id", "as_of"),
        Index("ix_context_packs_profile_as_of", "task_profile_id", "as_of"),
        Index("ix_context_packs_comparison", "comparison_pack_id"),
        {"schema": V3_SCHEMA},
    )

    context_pack_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    context_level: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.task_profiles.task_profile_id"), nullable=False
    )
    task_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    builder_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    universe_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.universe_snapshots.snapshot_id"), nullable=False
    )
    feature_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.feature_runs.feature_run_id"), nullable=False
    )
    recall_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.recall_runs.recall_run_id")
    )
    regime_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.market_regime_snapshots.regime_snapshot_id")
    )
    comparison_pack_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.candidate_comparison_packs.comparison_pack_id")
    )
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    missing_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    trim_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    references: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ContextEvidenceSelectionModel(Base):
    __tablename__ = "context_evidence_selections"
    __table_args__ = (
        CheckConstraint("side IN ('SUPPORT','CONTRARY','NEUTRAL')", name="valid_side"),
        CheckConstraint(
            "retrieval_score >= 0 AND retrieval_score <= 1", name="retrieval_score_range"
        ),
        CheckConstraint("relevance >= 0 AND relevance <= 1", name="relevance_range"),
        CheckConstraint("source_priority >= 0", name="nonnegative_source_priority"),
        CheckConstraint("final_order >= 1", name="positive_final_order"),
        UniqueConstraint(
            "context_pack_id",
            "final_order",
            name="uq_context_evidence_selections_pack_order",
        ),
        Index("ix_context_evidence_selections_evidence", "evidence_id"),
        {"schema": V3_SCHEMA},
    )

    context_pack_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.context_packs.context_pack_id"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.evidence_records.evidence_id"), primary_key=True
    )
    evidence_known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    selection_reason: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    retrieval_score: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    relevance: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    final_order: Mapped[int] = mapped_column(Integer, nullable=False)


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
    import_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_imports.import_id")
    )
    bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_bundles.bundle_id")
    )
    atomic_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_atomic_groups.atomic_group_id")
    )
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


class RecallChannelModel(Base):
    __tablename__ = "recall_channels"
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_recall_channels_code_version"),
        UniqueConstraint("content_hash", name="uq_recall_channels_content_hash"),
        {"schema": V3_SCHEMA},
    )

    channel_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RecallRunModel(Base):
    __tablename__ = "recall_runs"
    __table_args__ = (
        CheckConstraint("known_at >= as_of", name="known_after_as_of"),
        CheckConstraint("status IN ('PUBLISHED','FAILED')", name="valid_status"),
        CheckConstraint(
            "expected_channel_count >= 1 AND successful_channel_count >= 0 "
            "AND failed_channel_count >= 0 AND successful_channel_count + failed_channel_count = expected_channel_count",
            name="valid_channel_counts",
        ),
        CheckConstraint(
            "security_count >= 0 AND hit_security_count >= 0 AND hit_security_count <= security_count",
            name="valid_security_counts",
        ),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_range"),
        UniqueConstraint("content_hash", name="uq_recall_runs_content_hash"),
        Index("ix_recall_runs_as_of", "as_of", "status"),
        {"schema": V3_SCHEMA},
    )

    recall_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    feature_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.feature_runs.feature_run_id"), nullable=False)
    regime_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.market_regime_snapshots.regime_snapshot_id"))
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_channel_count: Mapped[int] = mapped_column(Integer, nullable=False)
    successful_channel_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_channel_count: Mapped[int] = mapped_column(Integer, nullable=False)
    security_count: Mapped[int] = mapped_column(Integer, nullable=False)
    hit_security_count: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    errors: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RecallResultModel(Base):
    __tablename__ = "recall_results"
    __table_args__ = (
        CheckConstraint("channel_rank >= 1", name="positive_rank"),
        CheckConstraint("strength >= 0 AND strength <= 1", name="strength_range"),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="coverage_range"),
        UniqueConstraint("recall_run_id", "channel_id", "security_id", name="uq_recall_results_run_channel_security"),
        UniqueConstraint("content_hash", name="uq_recall_results_content_hash"),
        Index("ix_recall_results_run_channel_rank", "recall_run_id", "channel_id", "channel_rank"),
        Index("ix_recall_results_run_security", "recall_run_id", "security_id"),
        {"schema": V3_SCHEMA},
    )

    recall_result_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    recall_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.recall_runs.recall_run_id"), nullable=False)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.recall_channels.channel_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    channel_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    strength: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    matched_features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coverage: Mapped[Decimal] = mapped_column(Numeric(8, 7), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RawOpportunityModel(Base):
    __tablename__ = "raw_opportunities"
    __table_args__ = (
        CheckConstraint("known_at >= as_of", name="known_after_as_of"),
        UniqueConstraint("recall_run_id", "security_id", name="uq_raw_opportunities_run_security"),
        UniqueConstraint("content_hash", name="uq_raw_opportunities_content_hash"),
        {"schema": V3_SCHEMA},
    )

    raw_opportunity_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    recall_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.recall_runs.recall_run_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recall_result_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    channel_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    reason_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PerformanceObservationModel(Base):
    __tablename__ = "performance_observations"
    __table_args__ = (
        CheckConstraint("horizon_sessions IN (3,5,10)", name="valid_horizon"),
        CheckConstraint("status IN ('PENDING','MATURED','UNAVAILABLE')", name="valid_status"),
        CheckConstraint("matures_at > as_of", name="future_maturity"),
        CheckConstraint("known_at >= as_of", name="known_after_as_of"),
        CheckConstraint("baseline_price > 0 AND (future_price IS NULL OR future_price > 0)", name="positive_prices"),
        CheckConstraint(
            "(status = 'PENDING' AND future_price IS NULL AND raw_return IS NULL "
            "AND benchmark_return IS NULL AND excess_return IS NULL "
            "AND unavailable_reason IS NULL AND supersedes_observation_id IS NULL) OR "
            "(status = 'MATURED' AND future_price IS NOT NULL AND raw_return IS NOT NULL "
            "AND unavailable_reason IS NULL AND supersedes_observation_id IS NOT NULL "
            "AND known_at >= matures_at) OR "
            "(status = 'UNAVAILABLE' AND future_price IS NULL AND raw_return IS NULL "
            "AND benchmark_return IS NULL AND excess_return IS NULL "
            "AND unavailable_reason IS NOT NULL AND supersedes_observation_id IS NOT NULL "
            "AND known_at >= matures_at)",
            name="status_payload",
        ),
        UniqueConstraint("supersedes_observation_id", name="uq_performance_observations_supersedes"),
        UniqueConstraint("content_hash", name="uq_performance_observations_content_hash"),
        Index(
            "uq_performance_observations_pending",
            "recall_run_id",
            "security_id",
            "horizon_sessions",
            unique=True,
            postgresql_where=text("supersedes_observation_id IS NULL"),
        ),
        Index("ix_performance_observations_maturity", "status", "matures_at"),
        {"schema": V3_SCHEMA},
    )

    observation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    recall_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.recall_runs.recall_run_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    horizon_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    matures_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    baseline_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    future_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    raw_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    benchmark_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    excess_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    unavailable_reason: Mapped[str | None] = mapped_column(String(256))
    supersedes_observation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.performance_observations.observation_id")
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RecallMissEvaluationModel(Base):
    __tablename__ = "recall_miss_evaluations"
    __table_args__ = (
        CheckConstraint("known_at >= evaluated_at", name="known_after_evaluated"),
        CheckConstraint(
            "(is_exceptional AND NOT was_recalled AND miss_type IS NOT NULL) OR "
            "((NOT is_exceptional OR was_recalled) AND miss_type IS NULL)",
            name="miss_type_consistency",
        ),
        UniqueConstraint("observation_id", "threshold_version", name="uq_recall_miss_observation_threshold"),
        UniqueConstraint("content_hash", name="uq_recall_miss_evaluations_content_hash"),
        {"schema": V3_SCHEMA},
    )

    evaluation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.performance_observations.observation_id"), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(64), nullable=False)
    threshold_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    was_recalled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_exceptional: Mapped[bool] = mapped_column(Boolean, nullable=False)
    miss_type: Mapped[str | None] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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


class AIResultImportModel(Base):
    __tablename__ = "ai_result_imports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PREVIEWED','CONFIRMED','PARTIAL_COMPLETED','FAILED')",
            name="valid_status",
        ),
        UniqueConstraint("bundle_hash", "preview_revision", name="uq_ai_import_bundle_revision"),
        UniqueConstraint("idempotency_key", name="uq_ai_import_idempotency"),
        {"schema": V3_SCHEMA},
    )

    import_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    bundle_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    preview_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    preview_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AIResultBundleModel(Base):
    __tablename__ = "ai_result_bundles"
    __table_args__ = (
        UniqueConstraint("bundle_hash", name="uq_ai_result_bundles_hash"),
        {"schema": V3_SCHEMA},
    )

    bundle_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_imports.import_id"), nullable=False, unique=True
    )
    agent_identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    task_run_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    produced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AIResultAtomicGroupModel(Base):
    __tablename__ = "ai_result_atomic_groups"
    __table_args__ = (
        CheckConstraint(
            "validation_status IN ('VALID','INVALID')", name="valid_validation_status"
        ),
        CheckConstraint(
            "commit_status IN ('PENDING','COMMITTED','FAILED')", name="valid_commit_status"
        ),
        UniqueConstraint("bundle_id", "group_key", name="uq_ai_atomic_groups_bundle_key"),
        UniqueConstraint("group_hash", name="uq_ai_atomic_groups_hash"),
        Index("ix_ai_atomic_groups_task_run", "task_run_id", "commit_status"),
        {"schema": V3_SCHEMA},
    )

    atomic_group_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    bundle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_bundles.bundle_id"), nullable=False
    )
    group_key: Mapped[str] = mapped_column(String(128), nullable=False)
    task_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.task_runs.task_run_id"), nullable=False
    )
    subject: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    group_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    commit_status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    retry_of_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_atomic_groups.atomic_group_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AIResultDependencyModel(Base):
    __tablename__ = "ai_result_dependencies"
    __table_args__ = (
        CheckConstraint("result_id <> depends_on_result_id", name="distinct_results"),
        {"schema": V3_SCHEMA},
    )

    result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_envelopes.result_id"), primary_key=True
    )
    depends_on_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_envelopes.result_id"), primary_key=True
    )


class WatchlistModel(Base):
    __tablename__ = "watchlists"
    __table_args__ = (
        UniqueConstraint("security_id", name="uq_watchlists_security"),
        Index("ix_watchlists_state", "state", "updated_at"),
        {"schema": V3_SCHEMA},
    )

    watchlist_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    latest_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WatchlistEventModel(Base):
    __tablename__ = "watchlist_events"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_watchlist_events_hash"),
        Index("ix_watchlist_events_watchlist", "watchlist_id", "event_time"),
        {"schema": V3_SCHEMA},
    )

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    watchlist_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.watchlists.watchlist_id"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_envelopes.result_id")
    )
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class WatchlistProposalModel(Base):
    __tablename__ = "watchlist_proposals"
    __table_args__ = (
        UniqueConstraint("source_result_id", name="uq_watchlist_proposals_source_result"),
        UniqueConstraint("content_hash", name="uq_watchlist_proposals_hash"),
        {"schema": V3_SCHEMA},
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False
    )
    source_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_envelopes.result_id"), nullable=False
    )
    proposed_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DecisionModel(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_decisions_hash"),
        Index("ix_decisions_security_as_of", "security_id", "as_of"),
        {"schema": V3_SCHEMA},
    )

    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.task_runs.task_run_id"), nullable=False
    )
    context_pack_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.context_packs.context_pack_id"), nullable=False
    )
    context_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_envelopes.result_id"), nullable=False, unique=True
    )
    agent_identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    original_entry_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    original_entry_plan_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    original_entry_plan_hash: Mapped[str | None] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    produced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class DecisionCorrectionModel(Base):
    __tablename__ = "decision_corrections"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_decision_corrections_hash"),
        {"schema": V3_SCHEMA},
    )

    correction_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.decisions.decision_id"), nullable=False
    )
    old_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    new_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_by: Mapped[str] = mapped_column(String(128), nullable=False)
    corrected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class EntryPlanModel(Base):
    __tablename__ = "entry_plans"
    __table_args__ = (
        CheckConstraint("version > 0", name="positive_version"),
        UniqueConstraint("decision_id", "version", name="uq_entry_plans_decision_version"),
        UniqueConstraint("content_hash", name="uq_entry_plans_hash"),
        Index("ix_entry_plans_decision_effective", "decision_id", "effective_from"),
        {"schema": V3_SCHEMA},
    )

    entry_plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.decisions.decision_id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_entry_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.entry_plans.entry_plan_id"), unique=True
    )
    created_by_review_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by_position_review_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_envelopes.result_id"), nullable=False
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ReviewModel(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_reviews_hash"),
        Index("ix_reviews_decision_as_of", "decision_id", "as_of"),
        {"schema": V3_SCHEMA},
    )

    review_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.decisions.decision_id"), nullable=False
    )
    previous_review_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.reviews.review_id"), unique=True
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.task_runs.task_run_id"), nullable=False
    )
    context_pack_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.context_packs.context_pack_id"), nullable=False
    )
    context_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_envelopes.result_id"), nullable=False, unique=True
    )
    agent_identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    thesis_status: Mapped[str] = mapped_column(String(32), nullable=False)
    time_efficiency: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class MarketReviewModel(Base):
    __tablename__ = "market_reviews"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_market_reviews_hash"),
        Index("ix_market_reviews_as_of", "as_of"),
        {"schema": V3_SCHEMA},
    )

    market_review_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    previous_market_review_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.market_reviews.market_review_id"), unique=True
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.task_runs.task_run_id"), nullable=False
    )
    market_regime_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.market_regime_snapshots.regime_snapshot_id")
    )
    context_pack_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.context_packs.context_pack_id"), nullable=False
    )
    context_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{V3_SCHEMA}.ai_result_envelopes.result_id"), nullable=False, unique=True
    )
    agent_identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    produced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AccountModel(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("name", name="uq_accounts_name"), {"schema": V3_SCHEMA})
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    cost_method: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ImageImportModel(Base):
    __tablename__ = "image_imports"
    __table_args__ = (UniqueConstraint("image_hash", name="uq_image_imports_hash"), {"schema": V3_SCHEMA})
    image_import_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    image_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    image_reference: Mapped[str] = mapped_column(Text, nullable=False)
    import_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ocr_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    field_regions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TradeDraftModel(Base):
    __tablename__ = "trade_drafts"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','CONFIRMED','REJECTED')", name="valid_status"),
        UniqueConstraint("idempotency_key", name="uq_trade_drafts_idempotency"),
        {"schema": V3_SCHEMA},
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    image_import_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.image_imports.image_import_id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    field_confidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PositionSnapshotDraftModel(Base):
    __tablename__ = "position_snapshot_drafts"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','CONFIRMED','REJECTED')", name="valid_status"),
        {"schema": V3_SCHEMA},
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    image_import_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.image_imports.image_import_id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    field_confidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confirmed_opening_position_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OpeningPositionModel(Base):
    __tablename__ = "opening_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "security_id", "baseline_time", name="uq_opening_positions_baseline"),
        UniqueConstraint("content_hash", name="uq_opening_positions_hash"),
        {"schema": V3_SCHEMA},
    )
    opening_position_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    baseline_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TradeLedgerModel(Base):
    __tablename__ = "trade_ledger"
    __table_args__ = (
        CheckConstraint("side IN ('BUY','SELL')", name="valid_side"),
        UniqueConstraint("idempotency_key", name="uq_trade_ledger_idempotency"),
        UniqueConstraint("content_hash", name="uq_trade_ledger_hash"),
        Index("ix_trade_ledger_position", "account_id", "security_id", "ledger_sequence"),
        {"schema": V3_SCHEMA},
    )
    trade_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    ledger_sequence: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.trade_drafts.draft_id"), unique=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    trade_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(Text)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.decisions.decision_id"))
    entry_plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.entry_plans.entry_plan_id"))
    entry_plan_version: Mapped[int | None] = mapped_column(Integer)
    execution_deviation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TradeCorrectionModel(Base):
    __tablename__ = "trade_corrections"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_trade_corrections_hash"), {"schema": V3_SCHEMA})
    correction_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.trade_ledger.trade_id"), nullable=False)
    correction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    replacement: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PortfolioAdjustmentModel(Base):
    __tablename__ = "portfolio_adjustments"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_portfolio_adjustments_hash"),
        Index("ix_portfolio_adjustments_position", "account_id", "security_id", "adjustment_sequence"),
        {"schema": V3_SCHEMA},
    )
    portfolio_adjustment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    adjustment_sequence: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    corporate_action_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.corporate_actions.corporate_action_id"))
    adjustment_type: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    cash_delta: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    cost_basis_delta: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.portfolio_adjustments.portfolio_adjustment_id"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ReconciliationModel(Base):
    __tablename__ = "reconciliations"
    __table_args__ = ({"schema": V3_SCHEMA},)
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    reconciled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    broker_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    projected_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    difference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class PositionProjectionModel(Base):
    __tablename__ = "position_projections"
    __table_args__ = ({"schema": V3_SCHEMA},)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), primary_key=True)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    cash_impact: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    last_ledger_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_adjustment_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    projection_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class PortfolioSnapshotModel(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_portfolio_snapshots_hash"), {"schema": V3_SCHEMA})
    portfolio_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    positions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    totals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class PortfolioPreferenceModel(Base):
    __tablename__ = "portfolio_preferences"
    __table_args__ = (UniqueConstraint("account_id", "version", name="uq_portfolio_preferences_version"), {"schema": V3_SCHEMA})
    preference_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class ActionCandidateModel(Base):
    __tablename__ = "action_candidates"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_action_candidates_hash"),
        Index("ix_action_candidates_security_as_of", "security_id", "as_of"),
        {"schema": V3_SCHEMA},
    )
    action_candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    raw_opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.raw_opportunities.raw_opportunity_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    task_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.task_runs.task_run_id"), nullable=False)
    context_pack_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.context_packs.context_pack_id"), nullable=False)
    context_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action_state: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    time_efficiency: Mapped[str] = mapped_column(String(16), nullable=False)
    time_efficiency_reason: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    contrary_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class EntryAssessmentModel(Base):
    __tablename__ = "entry_assessments"
    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_entry_assessments_hash"),
        Index("ix_entry_assessments_action_as_of", "action_candidate_id", "as_of"),
        {"schema": V3_SCHEMA},
    )
    entry_assessment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action_candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.action_candidates.action_candidate_id"), nullable=False)
    entry_plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.entry_plans.entry_plan_id"))
    readiness: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cancel_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    time_efficiency: Mapped[str] = mapped_column(String(16), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class PositionReviewModel(Base):
    __tablename__ = "position_reviews"
    __table_args__ = (
        UniqueConstraint("source_result_id", name="uq_position_reviews_source_result"),
        UniqueConstraint("content_hash", name="uq_position_reviews_hash"),
        Index("ix_position_reviews_position_as_of", "account_id", "security_id", "as_of"),
        {"schema": V3_SCHEMA},
    )
    position_review_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.accounts.account_id"), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.securities.security_id"), nullable=False)
    portfolio_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.portfolio_snapshots.portfolio_snapshot_id"))
    position_projection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.decisions.decision_id"))
    entry_plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.entry_plans.entry_plan_id"))
    previous_position_review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{V3_SCHEMA}.position_reviews.position_review_id"), unique=True)
    task_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.task_runs.task_run_id"), nullable=False)
    context_pack_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.context_packs.context_pack_id"), nullable=False)
    context_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_result_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{V3_SCHEMA}.ai_result_envelopes.result_id"), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    agent_identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity_snapshot: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    average_cost_snapshot: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    thesis_status: Mapped[str] = mapped_column(String(32), nullable=False)
    supporting_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    contrary_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    changed_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    new_risks: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    time_efficiency: Mapped[str] = mapped_column(String(16), nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
