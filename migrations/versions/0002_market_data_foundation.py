"""create V3 Phase 2 market data foundation

Revision ID: 0002_market_data_foundation
Revises: 0001_phase1_foundation
Create Date: 2026-08-29 22:30:00+08:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002_market_data_foundation"
down_revision: Union[str, Sequence[str], None] = "0001_phase1_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "v3"
IMMUTABLE_TABLES = (
    "universe_snapshots",
    "universe_members",
    "universe_diffs",
    "adjustment_factor_revisions",
    "adjustment_factors",
    "bar_series_revisions",
    "market_bars",
    "corporate_actions",
)


def upgrade() -> None:
    op.create_table(
        "securities",
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("security_type", sa.String(32), server_default="A_SHARE", nullable=False),
        sa.Column("list_date", sa.DateTime(timezone=True)),
        sa.Column("delist_date", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("security_id", name="pk_securities"),
        sa.UniqueConstraint("market", "code", name="uq_securities_market_code"),
        schema=SCHEMA,
    )
    op.create_table(
        "universe_sources",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("capability_version", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("source_id", name="pk_universe_sources"),
        sa.UniqueConstraint("code", name="uq_universe_sources_code"),
        schema=SCHEMA,
    )
    op.create_table(
        "universe_snapshots",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("coverage", sa.Numeric(6, 5), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("previous_snapshot_id", sa.Uuid()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= fetch_time", name="ck_universe_snapshots_known_after_fetch"),
        sa.CheckConstraint("coverage >= 0 AND coverage <= 1", name="ck_universe_snapshots_coverage_range"),
        sa.CheckConstraint("status IN ('PRIMARY','SECONDARY','LKG')", name="ck_universe_snapshots_valid_status"),
        sa.CheckConstraint("status <> 'LKG' OR stale", name="ck_universe_snapshots_lkg_requires_stale"),
        sa.ForeignKeyConstraint(["source_id"], [f"{SCHEMA}.universe_sources.source_id"], name="fk_universe_snapshots_source_id_universe_sources"),
        sa.ForeignKeyConstraint(["previous_snapshot_id"], [f"{SCHEMA}.universe_snapshots.snapshot_id"], name="fk_universe_snapshots_previous_snapshot_id_universe_snapshots"),
        sa.PrimaryKeyConstraint("snapshot_id", name="pk_universe_snapshots"),
        sa.UniqueConstraint("content_hash", name="uq_universe_snapshots_content_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_universe_snapshots_as_of", "universe_snapshots", ["as_of"], schema=SCHEMA)
    op.create_table(
        "universe_members",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("trading_status", sa.String(32), nullable=False),
        sa.Column("is_st", sa.Boolean(), nullable=False),
        sa.Column("suspended", sa.Boolean(), nullable=False),
        sa.Column("is_new_listing", sa.Boolean(), nullable=False),
        sa.Column("delisting_risk", sa.Boolean(), nullable=False),
        sa.Column("raw_reference", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], [f"{SCHEMA}.universe_snapshots.snapshot_id"], name="fk_universe_members_snapshot_id_universe_snapshots"),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"], name="fk_universe_members_security_id_securities"),
        sa.PrimaryKeyConstraint("snapshot_id", "security_id", name="pk_universe_members"),
        schema=SCHEMA,
    )
    op.create_index("ix_universe_members_security_snapshot", "universe_members", ["security_id", "snapshot_id"], schema=SCHEMA)
    op.create_table(
        "universe_diffs",
        sa.Column("diff_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("previous_snapshot_id", sa.Uuid()),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("change_type", sa.String(16), nullable=False),
        sa.Column("before_value", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("after_value", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("change_type IN ('ADDED','REMOVED','CHANGED')", name="ck_universe_diffs_valid_change_type"),
        sa.ForeignKeyConstraint(["snapshot_id"], [f"{SCHEMA}.universe_snapshots.snapshot_id"], name="fk_universe_diffs_snapshot_id_universe_snapshots"),
        sa.ForeignKeyConstraint(["previous_snapshot_id"], [f"{SCHEMA}.universe_snapshots.snapshot_id"], name="fk_universe_diffs_previous_snapshot_id_universe_snapshots"),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"], name="fk_universe_diffs_security_id_securities"),
        sa.PrimaryKeyConstraint("diff_id", name="pk_universe_diffs"),
        schema=SCHEMA,
    )
    op.create_index("ix_universe_diffs_snapshot", "universe_diffs", ["snapshot_id", "change_type"], schema=SCHEMA)
    op.create_table(
        "market_data_ingestion_runs",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_type", sa.String(64), nullable=False),
        sa.Column("universe_snapshot_id", sa.Uuid()),
        sa.Column("status", sa.String(16), server_default="PENDING", nullable=False),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("successful_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','PARTIAL','COMPLETED','FAILED')", name="ck_market_data_ingestion_runs_valid_status"),
        sa.CheckConstraint("expected_count >= 0", name="ck_market_data_ingestion_runs_nonnegative_expected"),
        sa.CheckConstraint("processed_count >= 0", name="ck_market_data_ingestion_runs_nonnegative_processed"),
        sa.CheckConstraint("successful_count >= 0", name="ck_market_data_ingestion_runs_nonnegative_successful"),
        sa.CheckConstraint("failed_count >= 0", name="ck_market_data_ingestion_runs_nonnegative_failed"),
        sa.CheckConstraint("processed_count = successful_count + failed_count", name="ck_ingestion_runs_processed_count_total"),
        sa.CheckConstraint("processed_count <= expected_count", name="ck_ingestion_runs_processed_not_over_expected"),
        sa.ForeignKeyConstraint(["universe_snapshot_id"], [f"{SCHEMA}.universe_snapshots.snapshot_id"], name="fk_ingestion_runs_universe_snapshot"),
        sa.PrimaryKeyConstraint("run_id", name="pk_market_data_ingestion_runs"),
        schema=SCHEMA,
    )
    op.create_table(
        "adjustment_factor_revisions",
        sa.Column("factor_revision_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("upstream_source", sa.String(128), nullable=False),
        sa.Column("derivation_method", sa.String(64), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("supersedes_revision_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= fetch_time", name="ck_adjustment_factor_revisions_known_after_fetch"),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"], name="fk_adjustment_factor_revisions_security_id_securities"),
        sa.ForeignKeyConstraint(["supersedes_revision_id"], [f"{SCHEMA}.adjustment_factor_revisions.factor_revision_id"], name="fk_factor_revisions_supersedes"),
        sa.PrimaryKeyConstraint("factor_revision_id", name="pk_adjustment_factor_revisions"),
        sa.UniqueConstraint("content_hash", name="uq_adjustment_factor_revisions_content_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_adjustment_factor_revisions_security_known", "adjustment_factor_revisions", ["security_id", "known_at"], schema=SCHEMA)
    op.create_table(
        "adjustment_factors",
        sa.Column("factor_revision_id", sa.Uuid(), nullable=False),
        sa.Column("trading_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("factor", sa.Numeric(24, 12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("factor > 0", name="ck_adjustment_factors_positive_factor"),
        sa.ForeignKeyConstraint(["factor_revision_id"], [f"{SCHEMA}.adjustment_factor_revisions.factor_revision_id"], name="fk_adjustment_factors_revision"),
        sa.PrimaryKeyConstraint("factor_revision_id", "trading_time", name="pk_adjustment_factors"),
        schema=SCHEMA,
    )
    op.create_table(
        "bar_series_revisions",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("adjust_type", sa.String(8), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("upstream_source", sa.String(128), nullable=False),
        sa.Column("raw_bar_available", sa.Boolean(), nullable=False),
        sa.Column("factor_revision_id", sa.Uuid()),
        sa.Column("point_in_time_precision", sa.String(16), nullable=False),
        sa.Column("precision_reason", sa.String(512)),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("supersedes_revision_id", sa.Uuid()),
        sa.Column("status", sa.String(16), server_default="PUBLISHED", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("period IN ('DAY','WEEK','MONTH')", name="ck_bar_series_revisions_valid_period"),
        sa.CheckConstraint("adjust_type IN ('RAW','QFQ','HFQ')", name="ck_bar_series_revisions_valid_adjust_type"),
        sa.CheckConstraint("point_in_time_precision IN ('FULL','LIMITED')", name="ck_bar_series_revisions_valid_precision"),
        sa.CheckConstraint("adjust_type <> 'RAW' OR raw_bar_available", name="ck_bar_revisions_raw_requires_available"),
        sa.CheckConstraint("point_in_time_precision <> 'LIMITED' OR precision_reason IS NOT NULL", name="ck_bar_series_revisions_limited_requires_reason"),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"], name="fk_bar_series_revisions_security_id_securities"),
        sa.ForeignKeyConstraint(["factor_revision_id"], [f"{SCHEMA}.adjustment_factor_revisions.factor_revision_id"], name="fk_bar_revisions_factor_revision"),
        sa.ForeignKeyConstraint(["supersedes_revision_id"], [f"{SCHEMA}.bar_series_revisions.revision_id"], name="fk_bar_revisions_supersedes"),
        sa.PrimaryKeyConstraint("revision_id", name="pk_bar_series_revisions"),
        sa.UniqueConstraint("content_hash", name="uq_bar_series_revisions_content_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_bar_series_revisions_security_period", "bar_series_revisions", ["security_id", "period", "adjust_type", "known_at"], schema=SCHEMA)
    op.create_table(
        "market_bars",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("bar_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(20, 6), nullable=False),
        sa.Column("high", sa.Numeric(20, 6), nullable=False),
        sa.Column("low", sa.Numeric(20, 6), nullable=False),
        sa.Column("close", sa.Numeric(20, 6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(24, 4), nullable=False),
        sa.Column("provisional", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_market_bars_positive_ohlc"),
        sa.CheckConstraint("high >= open AND high >= close AND high >= low", name="ck_market_bars_valid_high"),
        sa.CheckConstraint("low <= open AND low <= close AND low <= high", name="ck_market_bars_valid_low"),
        sa.CheckConstraint("volume >= 0", name="ck_market_bars_nonnegative_volume"),
        sa.CheckConstraint("amount >= 0", name="ck_market_bars_nonnegative_amount"),
        sa.CheckConstraint("NOT provisional", name="ck_market_bars_published_not_provisional"),
        sa.ForeignKeyConstraint(["revision_id"], [f"{SCHEMA}.bar_series_revisions.revision_id"], name="fk_market_bars_revision_id_bar_series_revisions"),
        sa.PrimaryKeyConstraint("revision_id", "bar_time", name="pk_market_bars"),
        schema=SCHEMA,
    )
    op.create_table(
        "corporate_actions",
        sa.Column("corporate_action_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("announcement_time", sa.DateTime(timezone=True)),
        sa.Column("record_time", sa.DateTime(timezone=True)),
        sa.Column("effective_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("evidence_id", sa.Uuid()),
        sa.Column("fetch_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("supersedes_action_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("known_at >= fetch_time", name="ck_corporate_actions_known_after_fetch"),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"], name="fk_corporate_actions_security_id_securities"),
        sa.ForeignKeyConstraint(["evidence_id"], [f"{SCHEMA}.evidence_records.evidence_id"], name="fk_corporate_actions_evidence_id_evidence_records"),
        sa.ForeignKeyConstraint(["supersedes_action_id"], [f"{SCHEMA}.corporate_actions.corporate_action_id"], name="fk_corporate_actions_supersedes_action_id_corporate_actions"),
        sa.PrimaryKeyConstraint("corporate_action_id", name="pk_corporate_actions"),
        sa.UniqueConstraint("content_hash", name="uq_corporate_actions_content_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_corporate_actions_security_effective", "corporate_actions", ["security_id", "effective_time"], schema=SCHEMA)

    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER prevent_mutation BEFORE UPDATE OR DELETE ON {SCHEMA}.{table_name} "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()"
        )


def downgrade() -> None:
    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS prevent_mutation ON {SCHEMA}.{table_name}")
    op.drop_index("ix_corporate_actions_security_effective", table_name="corporate_actions", schema=SCHEMA)
    op.drop_table("corporate_actions", schema=SCHEMA)
    op.drop_table("market_bars", schema=SCHEMA)
    op.drop_index("ix_bar_series_revisions_security_period", table_name="bar_series_revisions", schema=SCHEMA)
    op.drop_table("bar_series_revisions", schema=SCHEMA)
    op.drop_table("adjustment_factors", schema=SCHEMA)
    op.drop_index("ix_adjustment_factor_revisions_security_known", table_name="adjustment_factor_revisions", schema=SCHEMA)
    op.drop_table("adjustment_factor_revisions", schema=SCHEMA)
    op.drop_table("market_data_ingestion_runs", schema=SCHEMA)
    op.drop_index("ix_universe_diffs_snapshot", table_name="universe_diffs", schema=SCHEMA)
    op.drop_table("universe_diffs", schema=SCHEMA)
    op.drop_index("ix_universe_members_security_snapshot", table_name="universe_members", schema=SCHEMA)
    op.drop_table("universe_members", schema=SCHEMA)
    op.drop_index("ix_universe_snapshots_as_of", table_name="universe_snapshots", schema=SCHEMA)
    op.drop_table("universe_snapshots", schema=SCHEMA)
    op.drop_table("universe_sources", schema=SCHEMA)
    op.drop_table("securities", schema=SCHEMA)
