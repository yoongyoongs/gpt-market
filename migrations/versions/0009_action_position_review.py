"""create V3 Phase 9 action, entry and position review

Revision ID: 0009_action_position_review
Revises: 0008_trade_portfolio
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_action_position_review"
down_revision = "0008_trade_portfolio"
branch_labels = None
depends_on = None
SCHEMA = "v3"
JSON = postgresql.JSONB(astext_type=sa.Text())


def u(name, *, pk=False, nullable=False):
    return sa.Column(name, sa.Uuid(), primary_key=pk, nullable=False if pk else nullable)


def upgrade():
    op.create_table("action_candidates", u("action_candidate_id", pk=True),
        u("raw_opportunity_id"), u("security_id"), u("task_run_id"), u("context_pack_id"),
        sa.Column("context_pack_hash", sa.String(64), nullable=False),
        sa.Column("action_state", sa.String(16), nullable=False),
        sa.Column("expected_horizon", sa.String(16), nullable=False),
        sa.Column("time_efficiency", sa.String(16), nullable=False),
        sa.Column("time_efficiency_reason", sa.Text(), nullable=False),
        sa.Column("supporting_facts", JSON, nullable=False), sa.Column("contrary_facts", JSON, nullable=False),
        sa.Column("conditions", JSON, nullable=False), sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["raw_opportunity_id"], [f"{SCHEMA}.raw_opportunities.raw_opportunity_id"]),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.ForeignKeyConstraint(["task_run_id"], [f"{SCHEMA}.task_runs.task_run_id"]),
        sa.ForeignKeyConstraint(["context_pack_id"], [f"{SCHEMA}.context_packs.context_pack_id"]),
        sa.CheckConstraint("action_state IN ('OBSERVE','ACTIONABLE','DEFERRED','INVALIDATED')", name="valid_action_state"),
        sa.UniqueConstraint("content_hash", name="uq_action_candidates_hash"), schema=SCHEMA)
    op.create_index("ix_action_candidates_security_as_of", "action_candidates", ["security_id", "as_of"], schema=SCHEMA)
    op.create_table("entry_assessments", u("entry_assessment_id", pk=True), u("action_candidate_id"),
        u("entry_plan_id", nullable=True), sa.Column("readiness", sa.String(16), nullable=False),
        sa.Column("trigger_facts", JSON, nullable=False), sa.Column("cancel_facts", JSON, nullable=False),
        sa.Column("time_efficiency", sa.String(16), nullable=False), sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["action_candidate_id"], [f"{SCHEMA}.action_candidates.action_candidate_id"]),
        sa.ForeignKeyConstraint(["entry_plan_id"], [f"{SCHEMA}.entry_plans.entry_plan_id"]),
        sa.CheckConstraint("readiness IN ('NOT_READY','WAIT_TRIGGER','READY','CANCELLED')", name="valid_readiness"),
        sa.UniqueConstraint("content_hash", name="uq_entry_assessments_hash"), schema=SCHEMA)
    op.create_index("ix_entry_assessments_action_as_of", "entry_assessments", ["action_candidate_id", "as_of"], schema=SCHEMA)
    op.create_table("position_reviews", u("position_review_id", pk=True), u("account_id"), u("security_id"),
        u("portfolio_snapshot_id", nullable=True), sa.Column("position_projection_hash", sa.String(64), nullable=False),
        u("decision_id", nullable=True), u("entry_plan_id", nullable=True), u("previous_position_review_id", nullable=True),
        u("task_run_id"), u("context_pack_id"), sa.Column("context_pack_hash", sa.String(64), nullable=False),
        u("source_result_id"), sa.Column("evidence_ids", JSON, nullable=False), sa.Column("agent_identity", JSON, nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity_snapshot", sa.Numeric(24, 6), nullable=False),
        sa.Column("average_cost_snapshot", sa.Numeric(20, 6), nullable=False),
        sa.Column("thesis_status", sa.String(32), nullable=False),
        sa.Column("supporting_evidence", JSON, nullable=False), sa.Column("contrary_evidence", JSON, nullable=False),
        sa.Column("changed_facts", JSON, nullable=False), sa.Column("new_risks", JSON, nullable=False),
        sa.Column("time_efficiency", sa.String(16), nullable=False),
        sa.Column("recommended_action", sa.String(32), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.ForeignKeyConstraint(["portfolio_snapshot_id"], [f"{SCHEMA}.portfolio_snapshots.portfolio_snapshot_id"]),
        sa.ForeignKeyConstraint(["decision_id"], [f"{SCHEMA}.decisions.decision_id"]),
        sa.ForeignKeyConstraint(["entry_plan_id"], [f"{SCHEMA}.entry_plans.entry_plan_id"]),
        sa.ForeignKeyConstraint(["previous_position_review_id"], [f"{SCHEMA}.position_reviews.position_review_id"]),
        sa.ForeignKeyConstraint(["task_run_id"], [f"{SCHEMA}.task_runs.task_run_id"]),
        sa.ForeignKeyConstraint(["context_pack_id"], [f"{SCHEMA}.context_packs.context_pack_id"]),
        sa.ForeignKeyConstraint(["source_result_id"], [f"{SCHEMA}.ai_result_envelopes.result_id"]),
        sa.UniqueConstraint("source_result_id", name="uq_position_reviews_source_result"),
        sa.UniqueConstraint("previous_position_review_id", name="uq_position_reviews_previous"),
        sa.UniqueConstraint("content_hash", name="uq_position_reviews_hash"), schema=SCHEMA)
    op.create_index("ix_position_reviews_position_as_of", "position_reviews", ["account_id", "security_id", "as_of"], schema=SCHEMA)
    for table in ("action_candidates", "entry_assessments", "position_reviews"):
        op.execute(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()")


def downgrade():
    for table in ("position_reviews", "entry_assessments", "action_candidates"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {SCHEMA}.{table}")
        op.drop_table(table, schema=SCHEMA)
