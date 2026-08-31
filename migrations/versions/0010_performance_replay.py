"""create V3 Phase 10 performance, replay and regression

Revision ID: 0010_performance_replay
Revises: 0009_action_position_review
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_performance_replay"
down_revision = "0009_action_position_review"
branch_labels = None
depends_on = None
SCHEMA = "v3"
JSON = postgresql.JSONB(astext_type=sa.Text())
ABILITIES = "'SELECTION','INITIAL_ENTRY','USER_EXECUTION','ADD','REDUCE','FINAL_EXIT','RISK_CONTROL'"


def u(name, *, pk=False, nullable=False):
    return sa.Column(name, sa.Uuid(), primary_key=pk, nullable=False if pk else nullable)


def upgrade():
    op.create_table("performance_attributions", u("attribution_id", pk=True),
        sa.Column("ability", sa.String(32), nullable=False), sa.Column("subject_type", sa.String(32), nullable=False),
        u("subject_id"), sa.Column("strategy_version", sa.String(64), nullable=False),
        u("decision_id", nullable=True), u("original_entry_plan_id", nullable=True),
        u("evaluated_entry_plan_id", nullable=True), u("trade_id", nullable=True),
        u("trade_bound_entry_plan_id", nullable=True), u("regime_snapshot_id", nullable=True),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("matures_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_return", sa.Numeric(18, 10)), sa.Column("excess_return", sa.Numeric(18, 10)),
        sa.Column("mfe", sa.Numeric(18, 10)), sa.Column("mae", sa.Numeric(18, 10)),
        sa.Column("target_hit", sa.Boolean()), sa.Column("stop_hit", sa.Boolean()),
        sa.Column("metrics", JSON, nullable=False), sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], [f"{SCHEMA}.decisions.decision_id"]),
        sa.ForeignKeyConstraint(["original_entry_plan_id"], [f"{SCHEMA}.entry_plans.entry_plan_id"]),
        sa.ForeignKeyConstraint(["evaluated_entry_plan_id"], [f"{SCHEMA}.entry_plans.entry_plan_id"]),
        sa.ForeignKeyConstraint(["trade_id"], [f"{SCHEMA}.trade_ledger.trade_id"]),
        sa.ForeignKeyConstraint(["trade_bound_entry_plan_id"], [f"{SCHEMA}.entry_plans.entry_plan_id"]),
        sa.ForeignKeyConstraint(["regime_snapshot_id"], [f"{SCHEMA}.market_regime_snapshots.regime_snapshot_id"]),
        sa.CheckConstraint(f"ability IN ({ABILITIES})", name="valid_ability"),
        sa.CheckConstraint("matures_at > as_of AND known_at >= matures_at", name="mature_point_in_time"),
        sa.CheckConstraint("(trade_bound_entry_plan_id IS NULL) OR (trade_id IS NOT NULL)", name="trade_bound_plan_requires_trade"),
        sa.UniqueConstraint("content_hash", name="uq_performance_attributions_hash"), schema=SCHEMA)
    op.create_index("ix_performance_attributions_ability_regime", "performance_attributions", ["ability", "regime_snapshot_id", "matures_at"], schema=SCHEMA)
    op.create_table("performance_summaries", u("performance_summary_id", pk=True),
        sa.Column("ability", sa.String(32), nullable=False), sa.Column("regime_key", sa.String(128), nullable=False),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False), sa.Column("metrics", JSON, nullable=False),
        sa.Column("source_attribution_ids", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(f"ability IN ({ABILITIES})", name="valid_ability"),
        sa.CheckConstraint("window_end >= window_start AND sample_count >= 0", name="valid_window_samples"),
        sa.UniqueConstraint("ability", "regime_key", "strategy_version", "window_end", name="uq_performance_summaries_group"),
        sa.UniqueConstraint("content_hash", name="uq_performance_summaries_hash"), schema=SCHEMA)
    op.create_table("replay_runs", u("replay_run_id", pk=True),
        sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("replay_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_set", JSON, nullable=False), sa.Column("parameters", JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("leakage_checks", JSON, nullable=False),
        sa.Column("result", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('COMPLETED','BLOCKED')", name="valid_status"),
        sa.UniqueConstraint("content_hash", name="uq_replay_runs_hash"), schema=SCHEMA)
    op.create_table("regression_cases", u("regression_case_id", pk=True),
        sa.Column("name", sa.String(128), nullable=False), sa.Column("strategy_version", sa.String(64), nullable=False),
        sa.Column("replay_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_requirements", JSON, nullable=False), sa.Column("expected_invariants", JSON, nullable=False),
        u("source_replay_run_id", nullable=True), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("blocked_reason", sa.Text()), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["source_replay_run_id"], [f"{SCHEMA}.replay_runs.replay_run_id"]),
        sa.CheckConstraint("status IN ('ACTIVE','BLOCKED')", name="valid_status"),
        sa.UniqueConstraint("content_hash", name="uq_regression_cases_hash"), schema=SCHEMA)
    op.create_table("recall_miss_runs", u("recall_miss_run_id", pk=True),
        sa.Column("threshold_version", sa.String(64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("matured_count", sa.Integer(), nullable=False),
        sa.Column("unavailable_count", sa.Integer(), nullable=False),
        sa.Column("evaluation_count", sa.Integer(), nullable=False),
        sa.Column("miss_count", sa.Integer(), nullable=False),
        sa.Column("statistics", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("matured_count >= 0 AND unavailable_count >= 0 AND evaluation_count >= 0 AND miss_count >= 0", name="nonnegative_counts"),
        sa.UniqueConstraint("content_hash", name="uq_recall_miss_runs_hash"), schema=SCHEMA)
    for table in ("performance_attributions", "performance_summaries", "replay_runs", "regression_cases", "recall_miss_runs"):
        op.execute(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()")


def downgrade():
    for table in ("recall_miss_runs", "regression_cases", "replay_runs", "performance_summaries", "performance_attributions"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {SCHEMA}.{table}")
        op.drop_table(table, schema=SCHEMA)
