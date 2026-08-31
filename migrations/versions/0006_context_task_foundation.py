"""create V3 Phase 6 context and task foundation

Revision ID: 0006_context_task_foundation
Revises: 0005_multi_recall_foundation
Create Date: 2026-08-31 12:00:00+08:00
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Sequence, Union
from uuid import UUID

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006_context_task_foundation"
down_revision: Union[str, Sequence[str], None] = "0005_multi_recall_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
SCHEMA = "v3"
IMMUTABLE_TABLES = (
    "candidate_comparison_packs",
    "candidate_comparison_members",
    "context_packs",
    "context_evidence_selections",
    "task_profiles",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical datetime must include a timezone")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_value,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _backfill_task_hashes() -> None:
    connection = op.get_bind()
    profiles = connection.execute(
        sa.text(
            f"SELECT task_profile_id, profile_code, version, schedule, timezone, "
            f"trading_calendar_source, trading_calendar_version, context_level, "
            f"comparison_first, candidate_limit, topk_limit, topk_context_level, "
            f"output_schema, expected_group_count, grace_seconds, strategy_version, enabled "
            f"FROM {SCHEMA}.task_profiles"
        )
    ).mappings()
    for row in profiles:
        payload = dict(row)
        payload.pop("task_profile_id")
        content_hash = _canonical_hash(payload)
        connection.execute(
            sa.text(
                f"UPDATE {SCHEMA}.task_profiles SET content_hash=:content_hash "
                "WHERE task_profile_id=:task_profile_id"
            ),
            {
                "task_profile_id": row["task_profile_id"],
                "content_hash": content_hash,
            },
        )

    expected_runs = connection.execute(
        sa.text(
            f"SELECT expected_run_id, task_profile_id, task_profile_version, "
            f"scheduled_for, window_end, status FROM {SCHEMA}.expected_runs"
        )
    ).mappings()
    for row in expected_runs:
        content_hash = _canonical_hash(
            {
                "task_profile_id": row["task_profile_id"],
                "task_profile_version": row["task_profile_version"],
                "scheduled_for": row["scheduled_for"],
                "window_end": row["window_end"],
                "status": row["status"],
            }
        )
        connection.execute(
            sa.text(
                f"UPDATE {SCHEMA}.expected_runs SET content_hash=:content_hash "
                "WHERE expected_run_id=:expected_run_id"
            ),
            {
                "expected_run_id": row["expected_run_id"],
                "content_hash": content_hash,
            },
        )


def upgrade() -> None:
    op.create_table(
        "candidate_comparison_packs",
        sa.Column("comparison_pack_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_set_id", sa.Uuid(), nullable=False),
        sa.Column("builder_version", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("field_profile_version", sa.String(64), nullable=False),
        sa.Column("universe_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("feature_run_id", sa.Uuid(), nullable=False),
        sa.Column("recall_run_id", sa.Uuid()),
        sa.Column("regime_snapshot_id", sa.Uuid()),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("coverage", sa.Numeric(8, 7), nullable=False),
        sa.Column("missing_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trim_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "known_at >= as_of", name="known_after_as_of"
        ),
        sa.CheckConstraint(
            "candidate_count BETWEEN 20 AND 100",
            name="candidate_count_range",
        ),
        sa.CheckConstraint(
            "coverage >= 0 AND coverage <= 1",
            name="coverage_range",
        ),
        sa.ForeignKeyConstraint(
            ["universe_snapshot_id"],
            [f"{SCHEMA}.universe_snapshots.snapshot_id"],
            name="fk_candidate_comparison_packs_universe_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["feature_run_id"],
            [f"{SCHEMA}.feature_runs.feature_run_id"],
            name="fk_candidate_comparison_packs_feature_run",
        ),
        sa.ForeignKeyConstraint(
            ["recall_run_id"],
            [f"{SCHEMA}.recall_runs.recall_run_id"],
            name="fk_candidate_comparison_packs_recall_run",
        ),
        sa.ForeignKeyConstraint(
            ["regime_snapshot_id"],
            [f"{SCHEMA}.market_regime_snapshots.regime_snapshot_id"],
            name="fk_candidate_comparison_packs_regime_snapshot",
        ),
        sa.PrimaryKeyConstraint(
            "comparison_pack_id", name="pk_candidate_comparison_packs"
        ),
        sa.UniqueConstraint(
            "content_hash", name="uq_candidate_comparison_packs_content_hash"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_candidate_comparison_packs_as_of",
        "candidate_comparison_packs",
        ["as_of", "known_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_candidate_comparison_packs_feature_as_of",
        "candidate_comparison_packs",
        ["feature_run_id", "as_of"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_candidate_comparison_packs_recall",
        "candidate_comparison_packs",
        ["recall_run_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "candidate_comparison_members",
        sa.Column("comparison_pack_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_order", sa.Integer(), nullable=False),
        sa.Column("compact_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("coverage", sa.Numeric(8, 7), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("missing_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "candidate_order BETWEEN 1 AND 100",
            name="candidate_order_range",
        ),
        sa.CheckConstraint(
            "coverage >= 0 AND coverage <= 1",
            name="coverage_range",
        ),
        sa.ForeignKeyConstraint(
            ["comparison_pack_id"],
            [f"{SCHEMA}.candidate_comparison_packs.comparison_pack_id"],
            name="fk_candidate_comparison_members_pack",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            [f"{SCHEMA}.securities.security_id"],
            name="fk_candidate_comparison_members_security",
        ),
        sa.PrimaryKeyConstraint(
            "comparison_pack_id",
            "security_id",
            name="pk_candidate_comparison_members",
        ),
        sa.UniqueConstraint(
            "comparison_pack_id",
            "candidate_order",
            name="uq_candidate_comparison_members_pack_order",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_candidate_comparison_members_security",
        "candidate_comparison_members",
        ["security_id"],
        schema=SCHEMA,
    )

    op.add_column(
        "task_profiles",
        sa.Column(
            "trading_calendar_source",
            sa.String(128),
            server_default="UNKNOWN",
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "task_profiles",
        sa.Column(
            "trading_calendar_version",
            sa.String(64),
            server_default="UNKNOWN",
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "task_profiles",
        sa.Column(
            "comparison_first",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "task_profiles", sa.Column("candidate_limit", sa.Integer()), schema=SCHEMA
    )
    op.add_column(
        "task_profiles", sa.Column("topk_limit", sa.Integer()), schema=SCHEMA
    )
    op.add_column(
        "task_profiles", sa.Column("topk_context_level", sa.String(16)), schema=SCHEMA
    )
    op.add_column(
        "task_profiles",
        sa.Column(
            "strategy_version",
            sa.String(64),
            server_default="UNKNOWN",
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "task_profiles",
        sa.Column("pre_phase6_content_hash", sa.String(64)),
        schema=SCHEMA,
    )
    op.execute(
        f"UPDATE {SCHEMA}.task_profiles SET "
        "pre_phase6_content_hash=content_hash, "
        "trading_calendar_source=COALESCE(trading_calendar, 'UNKNOWN')"
    )
    op.create_check_constraint(
        "valid_comparison_settings",
        "task_profiles",
        "(comparison_first AND candidate_limit BETWEEN 20 AND 100 "
        "AND topk_limit BETWEEN 1 AND candidate_limit "
        "AND topk_context_level IN ('NORMAL','DEEP')) OR "
        "(NOT comparison_first AND candidate_limit IS NULL "
        "AND topk_limit IS NULL AND topk_context_level IS NULL)",
        schema=SCHEMA,
    )

    op.create_table(
        "context_packs",
        sa.Column("context_pack_id", sa.Uuid(), nullable=False),
        sa.Column("context_level", sa.String(16), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("task_profile_id", sa.Uuid(), nullable=False),
        sa.Column("task_profile_version", sa.Integer(), nullable=False),
        sa.Column("builder_version", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("universe_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("feature_run_id", sa.Uuid(), nullable=False),
        sa.Column("recall_run_id", sa.Uuid()),
        sa.Column("regime_snapshot_id", sa.Uuid()),
        sa.Column("comparison_pack_id", sa.Uuid()),
        sa.Column("token_budget", sa.Integer(), nullable=False),
        sa.Column("actual_tokens", sa.Integer(), nullable=False),
        sa.Column("coverage", sa.Numeric(8, 7), nullable=False),
        sa.Column("missing_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trim_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("references", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "context_level IN ('FAST','NORMAL','DEEP')",
            name="valid_level",
        ),
        sa.CheckConstraint(
            "subject_type IN ('SECURITY','MARKET')",
            name="valid_subject_type",
        ),
        sa.CheckConstraint(
            "task_profile_version > 0",
            name="positive_profile_version",
        ),
        sa.CheckConstraint(
            "known_at >= as_of", name="known_after_as_of"
        ),
        sa.CheckConstraint(
            "actual_tokens >= 0 AND actual_tokens <= token_budget",
            name="valid_token_count",
        ),
        sa.CheckConstraint(
            "(context_level = 'FAST' AND token_budget BETWEEN 2000 AND 4000) OR "
            "(context_level = 'NORMAL' AND token_budget BETWEEN 5000 AND 8000) OR "
            "(context_level = 'DEEP' AND token_budget BETWEEN 10000 AND 14000)",
            name="valid_token_budget",
        ),
        sa.CheckConstraint(
            "coverage >= 0 AND coverage <= 1",
            name="coverage_range",
        ),
        sa.ForeignKeyConstraint(
            ["task_profile_id"],
            [f"{SCHEMA}.task_profiles.task_profile_id"],
            name="fk_context_packs_task_profile",
        ),
        sa.ForeignKeyConstraint(
            ["universe_snapshot_id"],
            [f"{SCHEMA}.universe_snapshots.snapshot_id"],
            name="fk_context_packs_universe_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["feature_run_id"],
            [f"{SCHEMA}.feature_runs.feature_run_id"],
            name="fk_context_packs_feature_run",
        ),
        sa.ForeignKeyConstraint(
            ["recall_run_id"],
            [f"{SCHEMA}.recall_runs.recall_run_id"],
            name="fk_context_packs_recall_run",
        ),
        sa.ForeignKeyConstraint(
            ["regime_snapshot_id"],
            [f"{SCHEMA}.market_regime_snapshots.regime_snapshot_id"],
            name="fk_context_packs_regime_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["comparison_pack_id"],
            [f"{SCHEMA}.candidate_comparison_packs.comparison_pack_id"],
            name="fk_context_packs_comparison_pack",
        ),
        sa.PrimaryKeyConstraint("context_pack_id", name="pk_context_packs"),
        sa.UniqueConstraint("content_hash", name="uq_context_packs_content_hash"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_context_packs_subject_as_of",
        "context_packs",
        ["subject_type", "subject_id", "as_of"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_context_packs_profile_as_of",
        "context_packs",
        ["task_profile_id", "as_of"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_context_packs_comparison",
        "context_packs",
        ["comparison_pack_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "context_evidence_selections",
        sa.Column("context_pack_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("retrieval_score", sa.Numeric(8, 7), nullable=False),
        sa.Column("relevance", sa.Numeric(8, 7), nullable=False),
        sa.Column("source_priority", sa.Integer(), nullable=False),
        sa.Column("final_order", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "side IN ('SUPPORT','CONTRARY','NEUTRAL')",
            name="valid_side",
        ),
        sa.CheckConstraint(
            "retrieval_score >= 0 AND retrieval_score <= 1",
            name="retrieval_score_range",
        ),
        sa.CheckConstraint(
            "relevance >= 0 AND relevance <= 1",
            name="relevance_range",
        ),
        sa.CheckConstraint(
            "source_priority >= 0",
            name="nonnegative_source_priority",
        ),
        sa.CheckConstraint(
            "final_order >= 1",
            name="positive_final_order",
        ),
        sa.ForeignKeyConstraint(
            ["context_pack_id"],
            [f"{SCHEMA}.context_packs.context_pack_id"],
            name="fk_context_evidence_selections_context_pack",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            [f"{SCHEMA}.evidence_records.evidence_id"],
            name="fk_context_evidence_selections_evidence",
        ),
        sa.PrimaryKeyConstraint(
            "context_pack_id",
            "evidence_id",
            name="pk_context_evidence_selections",
        ),
        sa.UniqueConstraint(
            "context_pack_id",
            "final_order",
            name="uq_context_evidence_selections_pack_order",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_context_evidence_selections_evidence",
        "context_evidence_selections",
        ["evidence_id"],
        schema=SCHEMA,
    )

    op.add_column(
        "expected_runs", sa.Column("task_profile_version", sa.Integer()), schema=SCHEMA
    )
    op.add_column(
        "expected_runs", sa.Column("known_at", sa.DateTime(timezone=True)), schema=SCHEMA
    )
    op.add_column(
        "expected_runs", sa.Column("content_hash", sa.String(64)), schema=SCHEMA
    )
    op.add_column(
        "expected_runs",
        sa.Column("row_version", sa.BigInteger(), server_default="1", nullable=False),
        schema=SCHEMA,
    )
    op.execute(
        f"UPDATE {SCHEMA}.expected_runs AS expected SET "
        "task_profile_version=profile.version, known_at=expected.created_at "
        f"FROM {SCHEMA}.task_profiles AS profile "
        "WHERE profile.task_profile_id=expected.task_profile_id"
    )
    _backfill_task_hashes()
    for column in ("task_profile_version", "known_at", "content_hash"):
        op.alter_column("expected_runs", column, nullable=False, schema=SCHEMA)
    op.create_check_constraint(
        "valid_status",
        "expected_runs",
        "status IN ('EXPECTED','CANCELLED')",
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_expected_runs_content_hash",
        "expected_runs",
        ["content_hash"],
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_task_profiles_content_hash",
        "task_profiles",
        ["content_hash"],
        schema=SCHEMA,
    )

    op.add_column(
        "task_runs",
        sa.Column("task_profile_version", sa.Integer(), server_default="1"),
        schema=SCHEMA,
    )
    op.execute(
        f"UPDATE {SCHEMA}.task_runs AS run SET task_profile_version=profile.version "
        f"FROM {SCHEMA}.task_profiles AS profile "
        "WHERE profile.task_profile_id=run.task_profile_id"
    )
    op.alter_column("task_runs", "task_profile_version", nullable=False, schema=SCHEMA)
    op.create_foreign_key(
        "fk_task_runs_context_pack",
        "task_runs",
        "context_packs",
        ["context_pack_id"],
        ["context_pack_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_check_constraint(
        "status_count_consistency",
        "task_runs",
        "(status = 'COMPLETED' AND successful_group_count = expected_group_count) OR "
        "(status = 'PARTIAL_COMPLETED' AND successful_group_count > 0 "
        "AND successful_group_count < expected_group_count) OR "
        "(status IN ('PENDING_IMPORT','MISSED') AND successful_group_count = 0) OR "
        "status = 'CANCELLED'",
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

    op.drop_constraint(
        "status_count_consistency",
        "task_runs",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "fk_task_runs_context_pack",
        "task_runs",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_column("task_runs", "task_profile_version", schema=SCHEMA)

    op.drop_constraint(
        "uq_expected_runs_content_hash",
        "expected_runs",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "valid_status",
        "expected_runs",
        schema=SCHEMA,
        type_="check",
    )
    for column in ("row_version", "content_hash", "known_at", "task_profile_version"):
        op.drop_column("expected_runs", column, schema=SCHEMA)

    op.drop_constraint(
        "uq_task_profiles_content_hash",
        "task_profiles",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "valid_comparison_settings",
        "task_profiles",
        schema=SCHEMA,
        type_="check",
    )
    op.execute(
        f"UPDATE {SCHEMA}.task_profiles SET content_hash=pre_phase6_content_hash "
        "WHERE pre_phase6_content_hash IS NOT NULL"
    )
    for column in (
        "pre_phase6_content_hash",
        "strategy_version",
        "topk_context_level",
        "topk_limit",
        "candidate_limit",
        "comparison_first",
        "trading_calendar_version",
        "trading_calendar_source",
    ):
        op.drop_column("task_profiles", column, schema=SCHEMA)

    op.drop_index(
        "ix_context_evidence_selections_evidence",
        table_name="context_evidence_selections",
        schema=SCHEMA,
    )
    op.drop_table("context_evidence_selections", schema=SCHEMA)
    op.drop_index(
        "ix_context_packs_comparison", table_name="context_packs", schema=SCHEMA
    )
    op.drop_index(
        "ix_context_packs_profile_as_of", table_name="context_packs", schema=SCHEMA
    )
    op.drop_index(
        "ix_context_packs_subject_as_of", table_name="context_packs", schema=SCHEMA
    )
    op.drop_table("context_packs", schema=SCHEMA)
    op.drop_index(
        "ix_candidate_comparison_members_security",
        table_name="candidate_comparison_members",
        schema=SCHEMA,
    )
    op.drop_table("candidate_comparison_members", schema=SCHEMA)
    op.drop_index(
        "ix_candidate_comparison_packs_recall",
        table_name="candidate_comparison_packs",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_candidate_comparison_packs_feature_as_of",
        table_name="candidate_comparison_packs",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_candidate_comparison_packs_as_of",
        table_name="candidate_comparison_packs",
        schema=SCHEMA,
    )
    op.drop_table("candidate_comparison_packs", schema=SCHEMA)
