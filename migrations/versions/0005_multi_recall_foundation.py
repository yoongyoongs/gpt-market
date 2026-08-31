"""create V3 Phase 5 multi-recall foundation

Revision ID: 0005_multi_recall_foundation
Revises: 0004_evidence_ingestion
Create Date: 2026-08-31 03:00:00+08:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_multi_recall_foundation"
down_revision: Union[str, Sequence[str], None] = "0004_evidence_ingestion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
SCHEMA = "v3"
IMMUTABLE_TABLES = (
    "recall_channels",
    "recall_runs",
    "recall_results",
    "raw_opportunities",
    "performance_observations",
    "recall_miss_evaluations",
)


def upgrade() -> None:
    op.create_table(
        "recall_channels",
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", name="pk_recall_channels"),
        sa.UniqueConstraint("code", "version", name="uq_recall_channels_code_version"),
        sa.UniqueConstraint("content_hash", name="uq_recall_channels_content_hash"),
        schema=SCHEMA,
    )
    op.create_table(
        "recall_runs",
        sa.Column("recall_run_id", sa.Uuid(), nullable=False),
        sa.Column("feature_run_id", sa.Uuid(), nullable=False),
        sa.Column("regime_snapshot_id", sa.Uuid()),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("channel_set_hash", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expected_channel_count", sa.Integer(), nullable=False),
        sa.Column("successful_channel_count", sa.Integer(), nullable=False),
        sa.Column("failed_channel_count", sa.Integer(), nullable=False),
        sa.Column("security_count", sa.Integer(), nullable=False),
        sa.Column("hit_security_count", sa.Integer(), nullable=False),
        sa.Column("coverage", sa.Numeric(8, 7), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= as_of", name="ck_recall_runs_known_after_as_of"),
        sa.CheckConstraint("status IN ('PUBLISHED','FAILED')", name="ck_recall_runs_valid_status"),
        sa.CheckConstraint(
            "expected_channel_count >= 1 AND successful_channel_count >= 0 AND failed_channel_count >= 0 "
            "AND successful_channel_count + failed_channel_count = expected_channel_count",
            name="ck_recall_runs_valid_channel_counts",
        ),
        sa.CheckConstraint(
            "security_count >= 0 AND hit_security_count >= 0 AND hit_security_count <= security_count",
            name="ck_recall_runs_valid_security_counts",
        ),
        sa.CheckConstraint("coverage >= 0 AND coverage <= 1", name="ck_recall_runs_coverage_range"),
        sa.ForeignKeyConstraint(["feature_run_id"], [f"{SCHEMA}.feature_runs.feature_run_id"], name="fk_recall_runs_feature_run"),
        sa.ForeignKeyConstraint(["regime_snapshot_id"], [f"{SCHEMA}.market_regime_snapshots.regime_snapshot_id"], name="fk_recall_runs_regime"),
        sa.PrimaryKeyConstraint("recall_run_id", name="pk_recall_runs"),
        sa.UniqueConstraint("content_hash", name="uq_recall_runs_content_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_recall_runs_as_of", "recall_runs", ["as_of", "status"], schema=SCHEMA)
    op.create_table(
        "recall_results",
        sa.Column("recall_result_id", sa.Uuid(), nullable=False),
        sa.Column("recall_run_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("channel_rank", sa.Integer(), nullable=False),
        sa.Column("strength", sa.Numeric(8, 7), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("matched_features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("coverage", sa.Numeric(8, 7), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("channel_rank >= 1", name="ck_recall_results_positive_rank"),
        sa.CheckConstraint("strength >= 0 AND strength <= 1", name="ck_recall_results_strength_range"),
        sa.CheckConstraint("coverage >= 0 AND coverage <= 1", name="ck_recall_results_coverage_range"),
        sa.ForeignKeyConstraint(["recall_run_id"], [f"{SCHEMA}.recall_runs.recall_run_id"], name="fk_recall_results_run"),
        sa.ForeignKeyConstraint(["channel_id"], [f"{SCHEMA}.recall_channels.channel_id"], name="fk_recall_results_channel"),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"], name="fk_recall_results_security"),
        sa.PrimaryKeyConstraint("recall_result_id", name="pk_recall_results"),
        sa.UniqueConstraint("recall_run_id", "channel_id", "security_id", name="uq_recall_results_run_channel_security"),
        sa.UniqueConstraint("content_hash", name="uq_recall_results_content_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_recall_results_run_channel_rank", "recall_results", ["recall_run_id", "channel_id", "channel_rank"], schema=SCHEMA)
    op.create_index("ix_recall_results_run_security", "recall_results", ["recall_run_id", "security_id"], schema=SCHEMA)
    op.create_table(
        "raw_opportunities",
        sa.Column("raw_opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("recall_run_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recall_result_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("channel_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= as_of", name="ck_raw_opportunities_known_after_as_of"),
        sa.ForeignKeyConstraint(["recall_run_id"], [f"{SCHEMA}.recall_runs.recall_run_id"], name="fk_raw_opportunities_run"),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"], name="fk_raw_opportunities_security"),
        sa.PrimaryKeyConstraint("raw_opportunity_id", name="pk_raw_opportunities"),
        sa.UniqueConstraint("recall_run_id", "security_id", name="uq_raw_opportunities_run_security"),
        sa.UniqueConstraint("content_hash", name="uq_raw_opportunities_content_hash"),
        schema=SCHEMA,
    )
    op.create_table(
        "performance_observations",
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("recall_run_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("matures_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("future_price", sa.Numeric(20, 6)),
        sa.Column("raw_return", sa.Numeric(20, 10)),
        sa.Column("benchmark_return", sa.Numeric(20, 10)),
        sa.Column("excess_return", sa.Numeric(20, 10)),
        sa.Column("unavailable_reason", sa.String(256)),
        sa.Column("supersedes_observation_id", sa.Uuid()),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("horizon_sessions IN (3,5,10)", name="ck_performance_observations_valid_horizon"),
        sa.CheckConstraint("status IN ('PENDING','MATURED','UNAVAILABLE')", name="ck_performance_observations_valid_status"),
        sa.CheckConstraint("matures_at > as_of", name="ck_performance_observations_future_maturity"),
        sa.CheckConstraint("known_at >= as_of", name="ck_performance_observations_known_after_as_of"),
        sa.CheckConstraint("baseline_price > 0 AND (future_price IS NULL OR future_price > 0)", name="ck_performance_observations_positive_prices"),
        sa.CheckConstraint(
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
            name="ck_performance_observations_status_payload",
        ),
        sa.ForeignKeyConstraint(["recall_run_id"], [f"{SCHEMA}.recall_runs.recall_run_id"], name="fk_performance_observations_run"),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"], name="fk_performance_observations_security"),
        sa.ForeignKeyConstraint(["supersedes_observation_id"], [f"{SCHEMA}.performance_observations.observation_id"], name="fk_performance_observations_supersedes"),
        sa.PrimaryKeyConstraint("observation_id", name="pk_performance_observations"),
        sa.UniqueConstraint("supersedes_observation_id", name="uq_performance_observations_supersedes"),
        sa.UniqueConstraint("content_hash", name="uq_performance_observations_content_hash"),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_performance_observations_pending",
        "performance_observations",
        ["recall_run_id", "security_id", "horizon_sessions"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("supersedes_observation_id IS NULL"),
    )
    op.create_index("ix_performance_observations_maturity", "performance_observations", ["status", "matures_at"], schema=SCHEMA)
    op.create_table(
        "recall_miss_evaluations",
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("threshold_version", sa.String(64), nullable=False),
        sa.Column("threshold_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("was_recalled", sa.Boolean(), nullable=False),
        sa.Column("is_exceptional", sa.Boolean(), nullable=False),
        sa.Column("miss_type", sa.String(64)),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= evaluated_at", name="ck_recall_miss_known_after_evaluated"),
        sa.CheckConstraint(
            "(is_exceptional AND NOT was_recalled AND miss_type IS NOT NULL) OR "
            "((NOT is_exceptional OR was_recalled) AND miss_type IS NULL)",
            name="ck_recall_miss_type_consistency",
        ),
        sa.ForeignKeyConstraint(["observation_id"], [f"{SCHEMA}.performance_observations.observation_id"], name="fk_recall_miss_observation"),
        sa.PrimaryKeyConstraint("evaluation_id", name="pk_recall_miss_evaluations"),
        sa.UniqueConstraint("observation_id", "threshold_version", name="uq_recall_miss_observation_threshold"),
        sa.UniqueConstraint("content_hash", name="uq_recall_miss_evaluations_content_hash"),
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
    op.drop_table("recall_miss_evaluations", schema=SCHEMA)
    op.drop_index("ix_performance_observations_maturity", table_name="performance_observations", schema=SCHEMA)
    op.drop_index("uq_performance_observations_pending", table_name="performance_observations", schema=SCHEMA)
    op.drop_table("performance_observations", schema=SCHEMA)
    op.drop_table("raw_opportunities", schema=SCHEMA)
    op.drop_index("ix_recall_results_run_security", table_name="recall_results", schema=SCHEMA)
    op.drop_index("ix_recall_results_run_channel_rank", table_name="recall_results", schema=SCHEMA)
    op.drop_table("recall_results", schema=SCHEMA)
    op.drop_index("ix_recall_runs_as_of", table_name="recall_runs", schema=SCHEMA)
    op.drop_table("recall_runs", schema=SCHEMA)
    op.drop_table("recall_channels", schema=SCHEMA)
