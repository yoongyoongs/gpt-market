"""create V3 Phase 4 evidence ingestion pipeline

Revision ID: 0004_evidence_ingestion
Revises: 0003_full_market_features
Create Date: 2026-08-30 22:00:00+08:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004_evidence_ingestion"
down_revision: Union[str, Sequence[str], None] = "0003_full_market_features"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "v3"
IMMUTABLE_TABLES = (
    "raw_document_parse_attempts",
    "evidence_entity_links",
    "evidence_relations",
    "evidence_conflicts",
    "evidence_conflict_members",
)


def upgrade() -> None:
    op.add_column("evidence_sources", sa.Column("upstream_source", sa.String(128)), schema=SCHEMA)
    op.add_column(
        "evidence_sources",
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        schema=SCHEMA,
    )
    op.add_column("evidence_sources", sa.Column("priority", sa.Integer(), server_default="100", nullable=False), schema=SCHEMA)
    op.add_column("evidence_sources", sa.Column("rate_limit_per_minute", sa.Integer()), schema=SCHEMA)
    op.add_column("evidence_sources", sa.Column("parser_version", sa.String(64), server_default="v1", nullable=False), schema=SCHEMA)
    op.add_column("evidence_sources", sa.Column("reliability", sa.Numeric(5, 4), server_default="0.5000", nullable=False), schema=SCHEMA)
    op.create_check_constraint("ck_evidence_sources_positive_priority", "evidence_sources", "priority > 0", schema=SCHEMA)
    op.create_check_constraint(
        "ck_evidence_sources_positive_rate_limit",
        "evidence_sources",
        "rate_limit_per_minute IS NULL OR rate_limit_per_minute > 0",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_evidence_sources_reliability_range",
        "evidence_sources",
        "reliability >= 0 AND reliability <= 1",
        schema=SCHEMA,
    )

    op.drop_constraint("uq_raw_documents_content_hash", "raw_documents", schema=SCHEMA, type_="unique")
    op.add_column("raw_documents", sa.Column("document_key", sa.String(256)), schema=SCHEMA)
    op.add_column("raw_documents", sa.Column("normalized_reference", sa.Text()), schema=SCHEMA)
    op.add_column("raw_documents", sa.Column("payload_text", sa.Text()), schema=SCHEMA)
    op.add_column("raw_documents", sa.Column("payload_size", sa.BigInteger(), server_default="0", nullable=False), schema=SCHEMA)
    op.add_column("raw_documents", sa.Column("encoding", sa.String(32)), schema=SCHEMA)
    op.add_column(
        "raw_documents",
        sa.Column("response_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        schema=SCHEMA,
    )
    op.add_column("raw_documents", sa.Column("untrusted", sa.Boolean(), server_default=sa.text("true"), nullable=False), schema=SCHEMA)
    op.execute(
        f"UPDATE {SCHEMA}.raw_documents SET document_key=content_hash, normalized_reference=raw_reference "
        "WHERE document_key IS NULL"
    )
    op.alter_column("raw_documents", "document_key", nullable=False, schema=SCHEMA)
    op.alter_column("raw_documents", "normalized_reference", nullable=False, schema=SCHEMA)
    op.create_check_constraint("ck_raw_documents_nonnegative_payload_size", "raw_documents", "payload_size >= 0", schema=SCHEMA)
    op.create_unique_constraint(
        "uq_raw_documents_source_document_content",
        "raw_documents",
        ["evidence_source_id", "document_key", "content_hash"],
        schema=SCHEMA,
    )
    op.create_index("ix_raw_documents_source_document", "raw_documents", ["evidence_source_id", "document_key", "fetch_time"], schema=SCHEMA)
    op.create_index("ix_raw_documents_content_hash", "raw_documents", ["content_hash"], schema=SCHEMA)

    op.drop_constraint("uq_evidence_records_content_hash", "evidence_records", schema=SCHEMA, type_="unique")
    op.add_column("evidence_records", sa.Column("source_type", sa.String(32), server_default="VENDOR", nullable=False), schema=SCHEMA)
    op.add_column("evidence_records", sa.Column("claim_key", sa.String(256)), schema=SCHEMA)
    op.add_column(
        "evidence_records",
        sa.Column("normalized_payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        schema=SCHEMA,
    )
    op.add_column("evidence_records", sa.Column("decay_model", sa.String(32), server_default="NONE", nullable=False), schema=SCHEMA)
    op.add_column("evidence_records", sa.Column("decay_rate", sa.Numeric(10, 8)), schema=SCHEMA)
    op.add_column("evidence_records", sa.Column("availability", sa.String(32), server_default="AVAILABLE", nullable=False), schema=SCHEMA)
    op.add_column("evidence_records", sa.Column("untrusted", sa.Boolean(), server_default=sa.text("true"), nullable=False), schema=SCHEMA)
    op.add_column("evidence_records", sa.Column("supersedes_evidence_id", sa.Uuid()), schema=SCHEMA)
    op.execute(
        f"UPDATE {SCHEMA}.evidence_records SET claim_key=content_hash, normalized_payload=payload "
        "WHERE claim_key IS NULL"
    )
    op.alter_column("evidence_records", "claim_key", nullable=False, schema=SCHEMA)
    op.create_foreign_key(
        "fk_evidence_records_supersedes_evidence_id_evidence_records",
        "evidence_records",
        "evidence_records",
        ["supersedes_evidence_id"],
        ["evidence_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_evidence_records_valid_source_type",
        "evidence_records",
        "source_type IN ('OFFICIAL','VENDOR','NEWS','OPINION')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_evidence_records_valid_decay_model",
        "evidence_records",
        "decay_model IN ('NONE','LINEAR','EXPONENTIAL','FIXED_EXPIRY')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_evidence_records_nonnegative_decay_rate",
        "evidence_records",
        "decay_rate IS NULL OR decay_rate >= 0",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_evidence_records_valid_availability",
        "evidence_records",
        "availability IN ('AVAILABLE','EXPIRED','RETRACTED','SUPERSEDED')",
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_evidence_records_raw_parser_content",
        "evidence_records",
        ["raw_document_id", "parser_version", "content_hash"],
        schema=SCHEMA,
    )
    op.create_index("ix_evidence_records_claim", "evidence_records", ["subject_type", "subject_id", "claim_key", "known_at"], schema=SCHEMA)
    op.create_index("ix_evidence_records_retrieval", "evidence_records", ["subject_type", "subject_id", "availability", "expire_at", "known_at"], schema=SCHEMA)

    op.create_table(
        "evidence_fetch_runs",
        sa.Column("fetch_run_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_source_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True)),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("raw_inserted_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("parsed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("expected_count >= 0", name="ck_evidence_fetch_runs_nonnegative_expected"),
        sa.CheckConstraint("fetched_count >= 0", name="ck_evidence_fetch_runs_nonnegative_fetched"),
        sa.CheckConstraint("raw_inserted_count >= 0", name="ck_evidence_fetch_runs_nonnegative_inserted"),
        sa.CheckConstraint("duplicate_count >= 0", name="ck_evidence_fetch_runs_nonnegative_duplicates"),
        sa.CheckConstraint("parsed_count >= 0", name="ck_evidence_fetch_runs_nonnegative_parsed"),
        sa.CheckConstraint("failed_count >= 0", name="ck_evidence_fetch_runs_nonnegative_failed"),
        sa.CheckConstraint(
            "status IN ('RUNNING','PARTIAL','COMPLETED','FAILED')",
            name="ck_evidence_fetch_runs_valid_status",
        ),
        sa.CheckConstraint("completed_at IS NULL OR completed_at >= started_at", name="ck_evidence_fetch_runs_valid_completion"),
        sa.ForeignKeyConstraint(
            ["evidence_source_id"],
            [f"{SCHEMA}.evidence_sources.evidence_source_id"],
            name="fk_evidence_fetch_runs_evidence_source_id_evidence_sources",
        ),
        sa.PrimaryKeyConstraint("fetch_run_id", name="pk_evidence_fetch_runs"),
        schema=SCHEMA,
    )
    op.create_index("ix_evidence_fetch_runs_source_started", "evidence_fetch_runs", ["evidence_source_id", "started_at"], schema=SCHEMA)

    op.create_table(
        "raw_document_parse_attempts",
        sa.Column("parse_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("raw_document_id", sa.Uuid(), nullable=False),
        sa.Column("parser_code", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("output_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('SUCCESS','FAILED','SKIPPED')", name="ck_raw_document_parse_attempts_valid_status"),
        sa.CheckConstraint("output_count >= 0", name="ck_raw_document_parse_attempts_nonnegative_output"),
        sa.CheckConstraint("completed_at >= started_at", name="ck_raw_document_parse_attempts_valid_completion"),
        sa.ForeignKeyConstraint(
            ["raw_document_id"], [f"{SCHEMA}.raw_documents.raw_document_id"],
            name="fk_raw_document_parse_attempts_raw_document_id_raw_documents",
        ),
        sa.PrimaryKeyConstraint("parse_attempt_id", name="pk_raw_document_parse_attempts"),
        sa.UniqueConstraint("raw_document_id", "parser_code", "parser_version", name="uq_raw_document_parse_attempts_document_parser"),
        schema=SCHEMA,
    )

    op.create_table(
        "evidence_entity_links",
        sa.Column("entity_link_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("match_basis", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_evidence_entity_links_confidence_range"),
        sa.CheckConstraint("status IN ('CONFIRMED','CANDIDATE','REJECTED')", name="ck_evidence_entity_links_valid_status"),
        sa.ForeignKeyConstraint(
            ["evidence_id"], [f"{SCHEMA}.evidence_records.evidence_id"],
            name="fk_evidence_entity_links_evidence_id_evidence_records",
        ),
        sa.PrimaryKeyConstraint("entity_link_id", name="pk_evidence_entity_links"),
        sa.UniqueConstraint("evidence_id", "entity_type", "entity_id", name="uq_evidence_entity_links_evidence_entity"),
        schema=SCHEMA,
    )
    op.create_index("ix_evidence_entity_links_entity", "evidence_entity_links", ["entity_type", "entity_id", "status"], schema=SCHEMA)

    op.create_table(
        "evidence_relations",
        sa.Column("relation_id", sa.Uuid(), nullable=False),
        sa.Column("from_evidence_id", sa.Uuid(), nullable=False),
        sa.Column("to_evidence_id", sa.Uuid(), nullable=False),
        sa.Column("relation_type", sa.String(32), nullable=False),
        sa.Column("similarity", sa.Numeric(6, 5)),
        sa.Column("reason", sa.Text()),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("from_evidence_id <> to_evidence_id", name="ck_evidence_relations_distinct_records"),
        sa.CheckConstraint("similarity IS NULL OR (similarity >= 0 AND similarity <= 1)", name="ck_evidence_relations_similarity_range"),
        sa.CheckConstraint(
            "relation_type IN ('EXACT_DUPLICATE','NEAR_DUPLICATE','SUPERSEDES','CORRECTS','SUPPORTS')",
            name="ck_evidence_relations_valid_type",
        ),
        sa.ForeignKeyConstraint(
            ["from_evidence_id"], [f"{SCHEMA}.evidence_records.evidence_id"],
            name="fk_evidence_relations_from_evidence_id_evidence_records",
        ),
        sa.ForeignKeyConstraint(
            ["to_evidence_id"], [f"{SCHEMA}.evidence_records.evidence_id"],
            name="fk_evidence_relations_to_evidence_id_evidence_records",
        ),
        sa.PrimaryKeyConstraint("relation_id", name="pk_evidence_relations"),
        sa.UniqueConstraint("from_evidence_id", "to_evidence_id", "relation_type", name="uq_evidence_relations_pair_type"),
        schema=SCHEMA,
    )

    op.create_table(
        "evidence_conflicts",
        sa.Column("conflict_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("claim_key", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("selected_evidence_id", sa.Uuid()),
        sa.Column("resolution", sa.Text()),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('OPEN','RESOLVED','ACKNOWLEDGED')", name="ck_evidence_conflicts_valid_status"),
        sa.ForeignKeyConstraint(
            ["selected_evidence_id"], [f"{SCHEMA}.evidence_records.evidence_id"],
            name="fk_evidence_conflicts_selected_evidence_id_evidence_records",
        ),
        sa.PrimaryKeyConstraint("conflict_id", name="pk_evidence_conflicts"),
        sa.UniqueConstraint("subject_type", "subject_id", "claim_key", "content_hash", name="uq_evidence_conflicts_claim_content"),
        schema=SCHEMA,
    )
    op.create_index("ix_evidence_conflicts_subject", "evidence_conflicts", ["subject_type", "subject_id", "status"], schema=SCHEMA)

    op.create_table(
        "evidence_conflict_members",
        sa.Column("conflict_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("source_priority", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("source_priority > 0", name="ck_evidence_conflict_members_positive_priority"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_evidence_conflict_members_confidence_range"),
        sa.ForeignKeyConstraint(
            ["conflict_id"], [f"{SCHEMA}.evidence_conflicts.conflict_id"],
            name="fk_evidence_conflict_members_conflict_id_evidence_conflicts",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"], [f"{SCHEMA}.evidence_records.evidence_id"],
            name="fk_evidence_conflict_members_evidence_id_evidence_records",
        ),
        sa.PrimaryKeyConstraint("conflict_id", "evidence_id", name="pk_evidence_conflict_members"),
        schema=SCHEMA,
    )

    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER prevent_mutation BEFORE UPDATE OR DELETE ON {SCHEMA}.{table_name} "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()"
        )


def downgrade() -> None:
    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS prevent_mutation ON {SCHEMA}.{table_name}")
    op.drop_table("evidence_conflict_members", schema=SCHEMA)
    op.drop_index("ix_evidence_conflicts_subject", table_name="evidence_conflicts", schema=SCHEMA)
    op.drop_table("evidence_conflicts", schema=SCHEMA)
    op.drop_table("evidence_relations", schema=SCHEMA)
    op.drop_index("ix_evidence_entity_links_entity", table_name="evidence_entity_links", schema=SCHEMA)
    op.drop_table("evidence_entity_links", schema=SCHEMA)
    op.drop_table("raw_document_parse_attempts", schema=SCHEMA)
    op.drop_index("ix_evidence_fetch_runs_source_started", table_name="evidence_fetch_runs", schema=SCHEMA)
    op.drop_table("evidence_fetch_runs", schema=SCHEMA)

    op.drop_index("ix_evidence_records_retrieval", table_name="evidence_records", schema=SCHEMA)
    op.drop_index("ix_evidence_records_claim", table_name="evidence_records", schema=SCHEMA)
    op.drop_constraint("uq_evidence_records_raw_parser_content", "evidence_records", schema=SCHEMA, type_="unique")
    for name in (
        "ck_evidence_records_valid_availability",
        "ck_evidence_records_nonnegative_decay_rate",
        "ck_evidence_records_valid_decay_model",
        "ck_evidence_records_valid_source_type",
    ):
        op.drop_constraint(name, "evidence_records", schema=SCHEMA, type_="check")
    op.drop_constraint(
        "fk_evidence_records_supersedes_evidence_id_evidence_records",
        "evidence_records",
        schema=SCHEMA,
        type_="foreignkey",
    )
    for column in (
        "supersedes_evidence_id", "untrusted", "availability", "decay_rate", "decay_model",
        "normalized_payload", "claim_key", "source_type",
    ):
        op.drop_column("evidence_records", column, schema=SCHEMA)
    op.create_unique_constraint("uq_evidence_records_content_hash", "evidence_records", ["content_hash"], schema=SCHEMA)

    op.drop_index("ix_raw_documents_content_hash", table_name="raw_documents", schema=SCHEMA)
    op.drop_index("ix_raw_documents_source_document", table_name="raw_documents", schema=SCHEMA)
    op.drop_constraint("uq_raw_documents_source_document_content", "raw_documents", schema=SCHEMA, type_="unique")
    op.drop_constraint("ck_raw_documents_nonnegative_payload_size", "raw_documents", schema=SCHEMA, type_="check")
    for column in (
        "untrusted", "response_metadata", "encoding", "payload_size", "payload_text",
        "normalized_reference", "document_key",
    ):
        op.drop_column("raw_documents", column, schema=SCHEMA)
    op.create_unique_constraint("uq_raw_documents_content_hash", "raw_documents", ["content_hash"], schema=SCHEMA)

    for name in (
        "ck_evidence_sources_reliability_range",
        "ck_evidence_sources_positive_rate_limit",
        "ck_evidence_sources_positive_priority",
    ):
        op.drop_constraint(name, "evidence_sources", schema=SCHEMA, type_="check")
    for column in (
        "reliability", "parser_version", "rate_limit_per_minute", "priority", "capabilities", "upstream_source",
    ):
        op.drop_column("evidence_sources", column, schema=SCHEMA)
