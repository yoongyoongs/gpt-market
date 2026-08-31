"""create V3 Phase 8 trade ledger and portfolio

Revision ID: 0008_trade_portfolio
Revises: 0007_ai_import_decision
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_trade_portfolio"
down_revision = "0007_ai_import_decision"
branch_labels = None
depends_on = None
SCHEMA = "v3"
JSON = postgresql.JSONB(astext_type=sa.Text())
IMMUTABLE = (
    "opening_positions", "trade_ledger", "trade_corrections",
    "portfolio_adjustments", "reconciliations", "portfolio_snapshots",
    "portfolio_preferences",
)


def u(name, *, pk=False, nullable=False):
    return sa.Column(name, sa.Uuid(), primary_key=pk, nullable=False if pk else nullable)


def upgrade():
    op.create_table("accounts", u("account_id", pk=True), sa.Column("name", sa.String(128), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False), sa.Column("cost_method", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), server_default="ACTIVE", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_accounts_name"), schema=SCHEMA)
    op.create_table("image_imports", u("image_import_id", pk=True),
        sa.Column("image_hash", sa.String(64), nullable=False), sa.Column("image_reference", sa.Text(), nullable=False),
        sa.Column("import_type", sa.String(32), nullable=False), sa.Column("ocr_payload", JSON, nullable=False),
        sa.Column("field_regions", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("image_hash", name="uq_image_imports_hash"), schema=SCHEMA)
    op.create_table("trade_drafts", u("draft_id", pk=True), u("account_id"), u("security_id"),
        u("image_import_id", nullable=True), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", JSON, nullable=False), sa.Column("field_confidence", JSON, nullable=False),
        sa.Column("idempotency_key", sa.String(128)), sa.Column("confirmed_by", sa.String(128)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.ForeignKeyConstraint(["image_import_id"], [f"{SCHEMA}.image_imports.image_import_id"]),
        sa.CheckConstraint("status IN ('DRAFT','CONFIRMED','REJECTED')", name="valid_status"),
        sa.UniqueConstraint("idempotency_key", name="uq_trade_drafts_idempotency"), schema=SCHEMA)
    op.create_table("position_snapshot_drafts", u("draft_id", pk=True), u("account_id"), u("security_id"),
        u("image_import_id", nullable=True), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", JSON, nullable=False), sa.Column("field_confidence", JSON, nullable=False),
        u("confirmed_opening_position_id", nullable=True), sa.Column("confirmed_by", sa.String(128)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.ForeignKeyConstraint(["image_import_id"], [f"{SCHEMA}.image_imports.image_import_id"]),
        sa.CheckConstraint("status IN ('DRAFT','CONFIRMED','REJECTED')", name="valid_status"), schema=SCHEMA)
    op.create_table("opening_positions", u("opening_position_id", pk=True), u("account_id"), u("security_id"),
        sa.Column("baseline_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 6), nullable=False), sa.Column("average_cost", sa.Numeric(20, 6), nullable=False),
        sa.Column("source", sa.String(64), nullable=False), sa.Column("confirmed_by", sa.String(128), nullable=False),
        sa.Column("evidence_ids", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.CheckConstraint("quantity >= 0 AND average_cost >= 0", name="nonnegative_opening"),
        sa.UniqueConstraint("account_id", "security_id", "baseline_time", name="uq_opening_positions_baseline"),
        sa.UniqueConstraint("content_hash", name="uq_opening_positions_hash"), schema=SCHEMA)
    op.create_table("trade_ledger", u("trade_id", pk=True),
        sa.Column("ledger_sequence", sa.BigInteger(), sa.Identity(), nullable=False), u("draft_id", nullable=True),
        u("account_id"), u("security_id"), sa.Column("side", sa.String(8), nullable=False),
        sa.Column("trade_time", sa.DateTime(timezone=True), nullable=False), sa.Column("price", sa.Numeric(20, 6), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 6), nullable=False), sa.Column("fee", sa.Numeric(20, 6), nullable=False),
        sa.Column("source", sa.String(64), nullable=False), sa.Column("source_reference", sa.Text()),
        u("decision_id", nullable=True), u("entry_plan_id", nullable=True), sa.Column("entry_plan_version", sa.Integer()),
        sa.Column("execution_deviation", JSON, nullable=False), sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("confirmed_by", sa.String(128), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], [f"{SCHEMA}.trade_drafts.draft_id"]),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.ForeignKeyConstraint(["decision_id"], [f"{SCHEMA}.decisions.decision_id"]),
        sa.ForeignKeyConstraint(["entry_plan_id"], [f"{SCHEMA}.entry_plans.entry_plan_id"]),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="valid_side"),
        sa.CheckConstraint("price > 0 AND quantity > 0 AND fee >= 0", name="positive_trade_values"),
        sa.CheckConstraint("(entry_plan_id IS NULL) = (entry_plan_version IS NULL)", name="complete_plan_binding"),
        sa.UniqueConstraint("ledger_sequence", name="uq_trade_ledger_sequence"),
        sa.UniqueConstraint("draft_id", name="uq_trade_ledger_draft"),
        sa.UniqueConstraint("idempotency_key", name="uq_trade_ledger_idempotency"),
        sa.UniqueConstraint("content_hash", name="uq_trade_ledger_hash"), schema=SCHEMA)
    op.create_index("ix_trade_ledger_position", "trade_ledger", ["account_id", "security_id", "ledger_sequence"], schema=SCHEMA)
    op.create_table("trade_corrections", u("correction_id", pk=True), u("trade_id"),
        sa.Column("correction_type", sa.String(16), nullable=False), sa.Column("replacement", JSON, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("confirmed_by", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["trade_id"], [f"{SCHEMA}.trade_ledger.trade_id"]),
        sa.UniqueConstraint("content_hash", name="uq_trade_corrections_hash"), schema=SCHEMA)
    op.create_table("portfolio_adjustments", u("portfolio_adjustment_id", pk=True),
        sa.Column("adjustment_sequence", sa.BigInteger(), sa.Identity(), nullable=False), u("account_id"), u("security_id"),
        u("corporate_action_id", nullable=True), sa.Column("adjustment_type", sa.String(32), nullable=False),
        sa.Column("effective_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity_delta", sa.Numeric(24, 6), nullable=False), sa.Column("cash_delta", sa.Numeric(24, 6), nullable=False),
        sa.Column("cost_basis_delta", sa.Numeric(24, 6), nullable=False), sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("source", sa.String(64), nullable=False), sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False), sa.Column("confirmation_status", sa.String(32), nullable=False),
        sa.Column("confirmed_by", sa.String(128)), sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        u("supersedes_adjustment_id", nullable=True), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.ForeignKeyConstraint(["corporate_action_id"], [f"{SCHEMA}.corporate_actions.corporate_action_id"]),
        sa.ForeignKeyConstraint(["supersedes_adjustment_id"], [f"{SCHEMA}.portfolio_adjustments.portfolio_adjustment_id"]),
        sa.UniqueConstraint("adjustment_sequence", name="uq_portfolio_adjustments_sequence"),
        sa.UniqueConstraint("content_hash", name="uq_portfolio_adjustments_hash"), schema=SCHEMA)
    op.create_index("ix_portfolio_adjustments_position", "portfolio_adjustments", ["account_id", "security_id", "adjustment_sequence"], schema=SCHEMA)
    op.create_table("reconciliations", u("reconciliation_id", pk=True), u("account_id"), u("security_id"),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=False), sa.Column("broker_facts", JSON, nullable=False),
        sa.Column("projected_facts", JSON, nullable=False), sa.Column("difference", JSON, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column("evidence_ids", JSON, nullable=False), sa.Column("confirmed_by", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.UniqueConstraint("content_hash", name="uq_reconciliations_hash"), schema=SCHEMA)
    op.create_table("position_projections", u("account_id", pk=True), u("security_id", pk=True),
        sa.Column("quantity", sa.Numeric(24, 6), nullable=False), sa.Column("cost_basis", sa.Numeric(24, 6), nullable=False),
        sa.Column("average_cost", sa.Numeric(20, 6), nullable=False), sa.Column("cash_impact", sa.Numeric(24, 6), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(24, 6), nullable=False), sa.Column("last_ledger_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_adjustment_sequence", sa.BigInteger(), nullable=False), sa.Column("projection_version", sa.BigInteger(), nullable=False),
        sa.Column("rebuilt_at", sa.DateTime(timezone=True), nullable=False), sa.Column("input_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]), schema=SCHEMA)
    op.create_table("portfolio_snapshots", u("portfolio_snapshot_id", pk=True), u("account_id"),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False), sa.Column("positions", JSON, nullable=False),
        sa.Column("totals", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.UniqueConstraint("content_hash", name="uq_portfolio_snapshots_hash"), schema=SCHEMA)
    op.create_table("portfolio_preferences", u("preference_id", pk=True), u("account_id"),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("preferences", JSON, nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], [f"{SCHEMA}.accounts.account_id"]),
        sa.CheckConstraint("version > 0", name="positive_version"),
        sa.UniqueConstraint("account_id", "version", name="uq_portfolio_preferences_version"),
        sa.UniqueConstraint("content_hash", name="uq_portfolio_preferences_hash"), schema=SCHEMA)
    for table in IMMUTABLE:
        op.execute(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()")


def downgrade():
    for table in reversed(IMMUTABLE):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {SCHEMA}.{table}")
    for table in ("portfolio_preferences", "portfolio_snapshots", "position_projections", "reconciliations", "portfolio_adjustments", "trade_corrections", "trade_ledger", "opening_positions", "position_snapshot_drafts", "trade_drafts", "image_imports", "accounts"):
        op.drop_table(table, schema=SCHEMA)
