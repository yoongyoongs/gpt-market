"""create V3 Phase 1 foundation

Revision ID: 0001_phase1_foundation
Revises:
Create Date: 2026-08-29 21:10:00+08:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_phase1_foundation"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "v3"
IMMUTABLE_TABLES = (
    "raw_documents",
    "evidence_records",
    "agent_tasks",
    "ai_result_envelopes",
    "audit_events",
)


def upgrade() -> None:
    op.execute(sa.schema.CreateSchema(SCHEMA))

    op.create_table(
        "evidence_sources",
        sa.Column("evidence_source_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("evidence_source_id", name="pk_evidence_sources"),
        sa.UniqueConstraint("code", name="uq_evidence_sources_code"),
        schema=SCHEMA,
    )
    op.create_table(
        "raw_documents",
        sa.Column("raw_document_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_source_id", sa.Uuid(), nullable=False),
        sa.Column("raw_reference", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text()),
        sa.Column("mime_type", sa.String(128)),
        sa.Column("fetch_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parser_status", sa.String(32), server_default="PENDING", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= fetch_time", name="ck_raw_documents_known_after_fetch"),
        sa.ForeignKeyConstraint(
            ["evidence_source_id"], [f"{SCHEMA}.evidence_sources.evidence_source_id"], name="fk_raw_documents_evidence_source_id_evidence_sources"
        ),
        sa.PrimaryKeyConstraint("raw_document_id", name="pk_raw_documents"),
        sa.UniqueConstraint("content_hash", name="uq_raw_documents_content_hash"),
        schema=SCHEMA,
    )
    op.create_table(
        "evidence_records",
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("raw_document_id", sa.Uuid()),
        sa.Column("evidence_type", sa.String(32), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("upstream_source", sa.String(256)),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True)),
        sa.Column("publish_time", sa.DateTime(timezone=True)),
        sa.Column("fetch_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("relevance", sa.Numeric(5, 4), nullable=False),
        sa.Column("expire_at", sa.DateTime(timezone=True)),
        sa.Column("conflict_state", sa.String(32), server_default="NONE", nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= fetch_time", name="ck_evidence_records_known_after_fetch"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_evidence_records_confidence_range"),
        sa.CheckConstraint("relevance >= 0 AND relevance <= 1", name="ck_evidence_records_relevance_range"),
        sa.ForeignKeyConstraint(
            ["raw_document_id"], [f"{SCHEMA}.raw_documents.raw_document_id"], name="fk_evidence_records_raw_document_id_raw_documents"
        ),
        sa.PrimaryKeyConstraint("evidence_id", name="pk_evidence_records"),
        sa.UniqueConstraint("content_hash", name="uq_evidence_records_content_hash"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_evidence_records_subject",
        "evidence_records",
        ["subject_type", "subject_id", "known_at"],
        schema=SCHEMA,
    )
    op.create_table(
        "task_profiles",
        sa.Column("task_profile_id", sa.Uuid(), nullable=False),
        sa.Column("profile_code", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schedule", sa.String(128)),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("trading_calendar", sa.String(128), server_default="UNKNOWN", nullable=False),
        sa.Column("context_level", sa.String(16), nullable=False),
        sa.Column("output_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected_group_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("grace_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_task_profiles_positive_version"),
        sa.CheckConstraint("expected_group_count > 0", name="ck_task_profiles_positive_expected_groups"),
        sa.CheckConstraint("grace_seconds >= 0", name="ck_task_profiles_nonnegative_grace"),
        sa.PrimaryKeyConstraint("task_profile_id", name="pk_task_profiles"),
        sa.UniqueConstraint("profile_code", "version", name="uq_task_profiles_code_version"),
        schema=SCHEMA,
    )
    op.create_table(
        "expected_runs",
        sa.Column("expected_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_profile_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), server_default="EXPECTED", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("window_end >= scheduled_for", name="ck_expected_runs_valid_window"),
        sa.ForeignKeyConstraint(
            ["task_profile_id"], [f"{SCHEMA}.task_profiles.task_profile_id"], name="fk_expected_runs_task_profile_id_task_profiles"
        ),
        sa.PrimaryKeyConstraint("expected_run_id", name="pk_expected_runs"),
        sa.UniqueConstraint("task_profile_id", "scheduled_for", name="uq_expected_runs_profile_schedule"),
        schema=SCHEMA,
    )
    op.create_table(
        "task_runs",
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("expected_run_id", sa.Uuid()),
        sa.Column("task_profile_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), server_default="PENDING_IMPORT", nullable=False),
        sa.Column("expected_group_count", sa.Integer(), nullable=False),
        sa.Column("successful_group_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_group_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pending_group_count", sa.Integer(), nullable=False),
        sa.Column("context_pack_id", sa.Uuid()),
        sa.Column("context_pack_hash", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("expected_group_count > 0", name="ck_task_runs_positive_expected_groups"),
        sa.CheckConstraint("successful_group_count >= 0", name="ck_task_runs_nonnegative_successful_groups"),
        sa.CheckConstraint("failed_group_count >= 0", name="ck_task_runs_nonnegative_failed_groups"),
        sa.CheckConstraint("pending_group_count >= 0", name="ck_task_runs_nonnegative_pending_groups"),
        sa.CheckConstraint(
            "expected_group_count = successful_group_count + failed_group_count + pending_group_count",
            name="ck_task_runs_group_count_total",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_IMPORT','PARTIAL_COMPLETED','COMPLETED','MISSED','CANCELLED')",
            name="ck_task_runs_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["expected_run_id"], [f"{SCHEMA}.expected_runs.expected_run_id"], name="fk_task_runs_expected_run_id_expected_runs"
        ),
        sa.ForeignKeyConstraint(
            ["task_profile_id"], [f"{SCHEMA}.task_profiles.task_profile_id"], name="fk_task_runs_task_profile_id_task_profiles"
        ),
        sa.PrimaryKeyConstraint("task_run_id", name="pk_task_runs"),
        sa.UniqueConstraint("expected_run_id", name="uq_task_runs_expected_run_id"),
        schema=SCHEMA,
    )
    op.create_table(
        "agent_tasks",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("subject", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("task_profile", sa.String(64), nullable=False),
        sa.Column("trigger_type", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("context_pack_id", sa.Uuid(), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("expected_result_type", sa.String(64), nullable=False),
        sa.Column("constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_run_id"], [f"{SCHEMA}.task_runs.task_run_id"], name="fk_agent_tasks_task_run_id_task_runs"
        ),
        sa.PrimaryKeyConstraint("task_id", name="pk_agent_tasks"),
        sa.UniqueConstraint("content_hash", name="uq_agent_tasks_content_hash"),
        schema=SCHEMA,
    )
    op.create_table(
        "ai_result_envelopes",
        sa.Column("result_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("result_type", sa.String(64), nullable=False),
        sa.Column("agent_type", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128)),
        sa.Column("context_pack_id", sa.Uuid(), nullable=False),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= produced_at", name="ck_ai_result_envelopes_known_after_produced"),
        sa.ForeignKeyConstraint(
            ["task_id"], [f"{SCHEMA}.agent_tasks.task_id"], name="fk_ai_result_envelopes_task_id_agent_tasks"
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"], [f"{SCHEMA}.task_runs.task_run_id"], name="fk_ai_result_envelopes_task_run_id_task_runs"
        ),
        sa.PrimaryKeyConstraint("result_id", name="pk_ai_result_envelopes"),
        sa.UniqueConstraint("content_hash", name="uq_ai_result_envelopes_content_hash"),
        schema=SCHEMA,
    )
    op.create_table(
        "audit_events",
        sa.Column("audit_id", sa.Uuid(), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128)),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("object_type", sa.String(64), nullable=False),
        sa.Column("object_id", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(128)),
        sa.Column("before_hash", sa.String(64)),
        sa.Column("after_hash", sa.String(64)),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("audit_id", name="pk_audit_events"),
        schema=SCHEMA,
    )
    op.create_index("ix_audit_events_object", "audit_events", ["object_type", "object_id", "event_time"], schema=SCHEMA)

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.prevent_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'immutable V3 record cannot be updated or deleted';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER prevent_mutation BEFORE UPDATE OR DELETE ON {SCHEMA}.{table_name} "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()"
        )


def downgrade() -> None:
    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS prevent_mutation ON {SCHEMA}.{table_name}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.prevent_mutation()")
    op.drop_index("ix_audit_events_object", table_name="audit_events", schema=SCHEMA)
    op.drop_table("audit_events", schema=SCHEMA)
    op.drop_table("ai_result_envelopes", schema=SCHEMA)
    op.drop_table("agent_tasks", schema=SCHEMA)
    op.drop_table("task_runs", schema=SCHEMA)
    op.drop_table("expected_runs", schema=SCHEMA)
    op.drop_table("task_profiles", schema=SCHEMA)
    op.drop_index("ix_evidence_records_subject", table_name="evidence_records", schema=SCHEMA)
    op.drop_table("evidence_records", schema=SCHEMA)
    op.drop_table("raw_documents", schema=SCHEMA)
    op.drop_table("evidence_sources", schema=SCHEMA)
    op.execute(sa.schema.DropSchema(SCHEMA))
