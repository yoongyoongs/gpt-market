"""create V3 Phase 3 full-market feature foundation

Revision ID: 0003_full_market_features
Revises: 0002_market_data_foundation
Create Date: 2026-08-30 18:00:00+08:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003_full_market_features"
down_revision: Union[str, Sequence[str], None] = "0002_market_data_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
SCHEMA = "v3"
IMMUTABLE_TABLES = ("feature_runs", "security_features", "market_regime_snapshots")


def upgrade() -> None:
    op.create_table(
        "feature_runs",
        sa.Column("feature_run_id", sa.Uuid(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("universe_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("feature_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("successful_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("coverage", sa.Numeric(8, 7), nullable=False),
        sa.Column("bar_revision_set_hash", sa.String(64), nullable=False),
        sa.Column("input_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('RUNNING','PUBLISHED','FAILED')", name="ck_feature_runs_valid_status"),
        sa.CheckConstraint("expected_count >= 0 AND successful_count >= 0 AND failed_count >= 0", name="ck_feature_runs_nonnegative_counts"),
        sa.CheckConstraint("successful_count + failed_count <= expected_count", name="ck_feature_runs_valid_counts"),
        sa.CheckConstraint("coverage >= 0 AND coverage <= 1", name="ck_feature_runs_coverage_range"),
        sa.ForeignKeyConstraint(["universe_snapshot_id"], [f"{SCHEMA}.universe_snapshots.snapshot_id"], name="fk_feature_runs_universe_snapshot"),
        sa.PrimaryKeyConstraint("feature_run_id", name="pk_feature_runs"),
        sa.UniqueConstraint("content_hash", name="uq_feature_runs_content_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_feature_runs_published_as_of", "feature_runs", ["status", "as_of"], schema=SCHEMA)

    typed_numeric = (
        "return_3d", "return_5d", "return_10d", "return_20d", "return_60d",
        "return_120d", "return_250d", "position_60d", "position_120d", "position_250d",
        "ma5", "ma10", "ma20", "ma60", "ma20_slope", "ma60_slope", "atr14",
        "atr_pct", "volatility20", "distance_60d_high", "distance_60d_low",
        "volume_ratio_5d", "relative_index_strength", "relative_industry_strength",
    )
    columns = [
        sa.Column("feature_run_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("series_revision_id", sa.Uuid(), nullable=False),
        sa.Column("factor_revision_id", sa.Uuid()),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close", sa.Numeric(20, 6), nullable=False),
    ]
    columns.extend(sa.Column(name, sa.Numeric(20, 10)) for name in typed_numeric)
    columns.extend([
        sa.Column("breakout_20d", sa.Boolean()),
        sa.Column("pullback_20d", sa.Boolean()),
        sa.Column("amount", sa.Numeric(24, 4)),
        sa.Column("volume_expansion", sa.Boolean()),
        sa.Column("coverage", sa.Numeric(8, 7), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("missing_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("quality", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("coverage >= 0 AND coverage <= 1", name="ck_security_features_coverage_range"),
        sa.ForeignKeyConstraint(["feature_run_id"], [f"{SCHEMA}.feature_runs.feature_run_id"], name="fk_security_features_feature_run"),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"], name="fk_security_features_security"),
        sa.ForeignKeyConstraint(["series_revision_id"], [f"{SCHEMA}.bar_series_revisions.revision_id"], name="fk_security_features_series_revision"),
        sa.ForeignKeyConstraint(["factor_revision_id"], [f"{SCHEMA}.adjustment_factor_revisions.factor_revision_id"], name="fk_security_features_factor_revision"),
        sa.PrimaryKeyConstraint("feature_run_id", "security_id", name="pk_security_features"),
        sa.UniqueConstraint("content_hash", name="uq_security_features_content_hash"),
    ])
    op.create_table("security_features", *columns, schema=SCHEMA)
    op.create_index("ix_security_features_run_return20", "security_features", ["feature_run_id", "return_20d", "security_id"], schema=SCHEMA)
    op.create_index("ix_security_features_run_position60", "security_features", ["feature_run_id", "position_60d", "security_id"], schema=SCHEMA)
    op.create_index("ix_security_features_run_amount", "security_features", ["feature_run_id", "amount", "security_id"], schema=SCHEMA)

    op.create_table(
        "market_regime_snapshots",
        sa.Column("regime_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("feature_run_id", sa.Uuid(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        *[sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=False) for name in (
            "index_states", "breadth", "turnover", "limit_structure", "size_style",
            "growth_value_style", "industry_rotation", "risk_appetite_facts",
            "domestic_risk_evidence_ids", "global_risk_evidence_ids",
        )],
        sa.Column("coverage", sa.Numeric(8, 7), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 7), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= as_of", name="ck_market_regime_snapshots_known_after_as_of"),
        sa.CheckConstraint("coverage >= 0 AND coverage <= 1", name="ck_market_regime_snapshots_coverage_range"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_market_regime_snapshots_confidence_range"),
        sa.ForeignKeyConstraint(["feature_run_id"], [f"{SCHEMA}.feature_runs.feature_run_id"], name="fk_market_regime_feature_run"),
        sa.PrimaryKeyConstraint("regime_snapshot_id", name="pk_market_regime_snapshots"),
        sa.UniqueConstraint("feature_run_id", name="uq_market_regime_feature_run"),
        sa.UniqueConstraint("content_hash", name="uq_market_regime_content_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_market_regime_as_of", "market_regime_snapshots", ["as_of"], schema=SCHEMA)
    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER prevent_mutation BEFORE UPDATE OR DELETE ON {SCHEMA}.{table_name} "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()"
        )


def downgrade() -> None:
    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS prevent_mutation ON {SCHEMA}.{table_name}")
    op.drop_index("ix_market_regime_as_of", table_name="market_regime_snapshots", schema=SCHEMA)
    op.drop_table("market_regime_snapshots", schema=SCHEMA)
    for name in ("ix_security_features_run_amount", "ix_security_features_run_position60", "ix_security_features_run_return20"):
        op.drop_index(name, table_name="security_features", schema=SCHEMA)
    op.drop_table("security_features", schema=SCHEMA)
    op.drop_index("ix_feature_runs_published_as_of", table_name="feature_runs", schema=SCHEMA)
    op.drop_table("feature_runs", schema=SCHEMA)
