"""candidate engine scan tables (design §35.1-§35.7)

Revision ID: 0018_candidate_scan_tables
Revises: 0017_regime_stale_reason
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA = "v3"

revision = "0018_candidate_scan_tables"
down_revision = "0017_regime_stale_reason"
branch_labels = None
depends_on = None

_JSON = (
    sa.JSON()
    .with_variant(JSONB(), "postgresql")
)


def upgrade() -> None:
    op.create_table(
        "scan_runs",
        sa.Column("scan_run_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("parameter_version", sa.String(64), nullable=False),
        sa.Column("universe_count", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("recall_count", sa.Integer(), nullable=False),
        sa.Column("pareto_count", sa.Integer(), nullable=False),
        sa.Column("machine_count", sa.Integer(), nullable=False),
        sa.Column("deep_count", sa.Integer(), nullable=False),
        sa.Column("final_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "universe_count >= 0 AND eligible_count >= 0 AND recall_count >= 0 "
            "AND pareto_count >= 0 AND machine_count >= 0 AND deep_count >= 0 "
            "AND final_count >= 0",
            name="valid_counts",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_scan_runs_market_date", "scan_runs", ["market_date", "status"], schema=SCHEMA,
    )

    op.create_table(
        "candidate_snapshots",
        sa.Column("snapshot_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_run_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("alive", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Numeric(12, 6), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("drop_reason", sa.String(128), nullable=True),
        sa.Column("feature_version", sa.String(64), nullable=False),
        sa.Column("data_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "stage IN ('UNIVERSE','SAFETY','RECALL','PARETO','MACHINE','DEEP','FINAL')",
            name="valid_stage",
        ),
        sa.ForeignKeyConstraint(
            ["scan_run_id"], [f"{SCHEMA}.scan_runs.scan_run_id"],
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_candidate_snapshots_run_stage_code",
        "candidate_snapshots", ["scan_run_id", "stage", "code"], schema=SCHEMA,
    )
    op.create_index(
        "ix_candidate_snapshots_run_code",
        "candidate_snapshots", ["scan_run_id", "code"], schema=SCHEMA,
    )

    op.create_table(
        "expert_recall_rows",
        sa.Column("row_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_run_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("expert", sa.String(8), nullable=False),
        sa.Column("score", sa.Numeric(8, 4), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("hit", sa.Boolean(), nullable=False),
        sa.Column("reason_json", _JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["scan_run_id"], [f"{SCHEMA}.scan_runs.scan_run_id"],
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_expert_recall_rows_run_expert",
        "expert_recall_rows", ["scan_run_id", "expert"], schema=SCHEMA,
    )

    op.create_table(
        "pareto_result_rows",
        sa.Column("row_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_run_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("front", sa.Integer(), nullable=False),
        sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("crowding_distance", sa.Numeric(16, 6), nullable=False),
        sa.Column("p_position", sa.Numeric(8, 4), nullable=True),
        sa.Column("p_transition", sa.Numeric(8, 4), nullable=True),
        sa.Column("p_accumulation", sa.Numeric(8, 4), nullable=True),
        sa.Column("p_quality_catalyst", sa.Numeric(8, 4), nullable=True),
        sa.Column("p_risk_reward", sa.Numeric(8, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["scan_run_id"], [f"{SCHEMA}.scan_runs.scan_run_id"],
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_pareto_result_rows_run", "pareto_result_rows", ["scan_run_id", "front"],
        schema=SCHEMA,
    )

    op.create_table(
        "outcome_labels",
        sa.Column("row_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_run_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("mfe_5", sa.Numeric(10, 6), nullable=True),
        sa.Column("mfe_10", sa.Numeric(10, 6), nullable=True),
        sa.Column("mfe_20", sa.Numeric(10, 6), nullable=True),
        sa.Column("mae_5", sa.Numeric(10, 6), nullable=True),
        sa.Column("mae_10", sa.Numeric(10, 6), nullable=True),
        sa.Column("mae_20", sa.Numeric(10, 6), nullable=True),
        sa.Column("time_to_8", sa.Integer(), nullable=True),
        sa.Column("time_to_10", sa.Integer(), nullable=True),
        sa.Column("time_to_15", sa.Integer(), nullable=True),
        sa.Column("close_t", sa.Numeric(20, 6), nullable=True),
        sa.Column("bars_used", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("label IS NULL OR label IN ('A','B','C')", name="valid_label"),
        sa.ForeignKeyConstraint(
            ["scan_run_id"], [f"{SCHEMA}.scan_runs.scan_run_id"],
        ),
        sa.UniqueConstraint("scan_run_id", "code", name="uq_outcome_labels_run_code"),
        schema=SCHEMA,
    )

    op.create_table(
        "miss_audit_rows",
        sa.Column("row_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_run_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("future_label", sa.String(8), nullable=False),
        sa.Column("last_alive_stage", sa.String(16), nullable=False),
        sa.Column("drop_stage", sa.String(16), nullable=False),
        sa.Column("drop_reason", sa.String(128), nullable=False),
        sa.Column("audit_json", _JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["scan_run_id"], [f"{SCHEMA}.scan_runs.scan_run_id"],
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_miss_audit_rows_run", "miss_audit_rows", ["scan_run_id", "future_label"],
        schema=SCHEMA,
    )

    op.create_table(
        "shadow_pool_rows",
        sa.Column("row_id", sa.Uuid(), primary_key=True),
        sa.Column("scan_run_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("sample_group", sa.String(32), nullable=False),
        sa.Column("drop_stage", sa.String(16), nullable=False),
        sa.Column("drop_reason", sa.String(128), nullable=False),
        sa.Column("outcome_label", sa.String(8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "sample_group IN ('near_miss','single_expert','random','other')",
            name="valid_sample_group",
        ),
        sa.ForeignKeyConstraint(
            ["scan_run_id"], [f"{SCHEMA}.scan_runs.scan_run_id"],
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_shadow_pool_rows_run_group", "shadow_pool_rows",
        ["scan_run_id", "sample_group"], schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("shadow_pool_rows", schema=SCHEMA)
    op.drop_table("miss_audit_rows", schema=SCHEMA)
    op.drop_table("outcome_labels", schema=SCHEMA)
    op.drop_table("pareto_result_rows", schema=SCHEMA)
    op.drop_table("expert_recall_rows", schema=SCHEMA)
    op.drop_table("candidate_snapshots", schema=SCHEMA)
    op.drop_index("ix_scan_runs_market_date", table_name="scan_runs", schema=SCHEMA)
    op.drop_table("scan_runs", schema=SCHEMA)
