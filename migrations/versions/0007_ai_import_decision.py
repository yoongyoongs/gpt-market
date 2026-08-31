"""create V3 Phase 7 AI import and decision state

Revision ID: 0007_ai_import_decision
Revises: 0006_context_task_foundation
Create Date: 2026-08-31 22:30:00+08:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007_ai_import_decision"
down_revision: Union[str, Sequence[str], None] = "0006_context_task_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
SCHEMA = "v3"
JSON = postgresql.JSONB(astext_type=sa.Text())
IMMUTABLE = (
    "ai_result_bundles", "ai_result_dependencies", "watchlist_events", "watchlist_proposals",
    "decisions", "decision_corrections", "entry_plans", "reviews", "market_reviews",
)


def _uuid(name: str, *, primary=False, nullable=False):
    return sa.Column(name, sa.Uuid(), primary_key=primary, nullable=nullable if not primary else False)


def _hash_unique(table: str):
    return sa.UniqueConstraint("content_hash", name=f"uq_{table}_hash")


def upgrade() -> None:
    op.create_table(
        "ai_result_imports", _uuid("import_id", primary=True), _uuid("bundle_id"),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("preview_revision", sa.Integer(), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("preview_payload", JSON, nullable=False),
        sa.Column("confirmed_by", sa.String(128)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("preview_revision > 0", name="positive_preview_revision"),
        sa.CheckConstraint("status IN ('PREVIEWED','CONFIRMED','PARTIAL_COMPLETED','FAILED')", name="valid_status"),
        sa.UniqueConstraint("bundle_hash", "preview_revision", name="uq_ai_import_bundle_revision"),
        sa.UniqueConstraint("idempotency_key", name="uq_ai_import_idempotency"), schema=SCHEMA,
    )
    op.create_table(
        "ai_result_bundles", _uuid("bundle_id", primary=True), _uuid("import_id"),
        sa.Column("agent_identity", JSON, nullable=False),
        sa.Column("task_run_ids", JSON, nullable=False),
        sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bundle_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], [f"{SCHEMA}.ai_result_imports.import_id"]),
        sa.UniqueConstraint("import_id", name="uq_ai_result_bundles_import"),
        sa.UniqueConstraint("bundle_hash", name="uq_ai_result_bundles_hash"), schema=SCHEMA,
    )
    op.create_table(
        "ai_result_atomic_groups", _uuid("atomic_group_id", primary=True), _uuid("bundle_id"),
        sa.Column("group_key", sa.String(128), nullable=False), _uuid("task_run_id"),
        sa.Column("subject", JSON, nullable=False), sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("group_hash", sa.String(64), nullable=False),
        sa.Column("validation_status", sa.String(16), nullable=False),
        sa.Column("commit_status", sa.String(16), nullable=False), sa.Column("error", sa.Text()),
        _uuid("retry_of_group_id", nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["bundle_id"], [f"{SCHEMA}.ai_result_bundles.bundle_id"]),
        sa.ForeignKeyConstraint(["task_run_id"], [f"{SCHEMA}.task_runs.task_run_id"]),
        sa.ForeignKeyConstraint(["retry_of_group_id"], [f"{SCHEMA}.ai_result_atomic_groups.atomic_group_id"]),
        sa.CheckConstraint("validation_status IN ('VALID','INVALID')", name="valid_validation_status"),
        sa.CheckConstraint("commit_status IN ('PENDING','COMMITTED','FAILED')", name="valid_commit_status"),
        sa.UniqueConstraint("bundle_id", "group_key", name="uq_ai_atomic_groups_bundle_key"),
        sa.UniqueConstraint("group_hash", name="uq_ai_atomic_groups_hash"), schema=SCHEMA,
    )
    op.create_index("ix_ai_atomic_groups_task_run", "ai_result_atomic_groups", ["task_run_id", "commit_status"], schema=SCHEMA)
    op.add_column("ai_result_envelopes", _uuid("import_id", nullable=True), schema=SCHEMA)
    op.add_column("ai_result_envelopes", _uuid("bundle_id", nullable=True), schema=SCHEMA)
    op.add_column("ai_result_envelopes", _uuid("atomic_group_id", nullable=True), schema=SCHEMA)
    op.create_foreign_key("fk_ai_envelopes_import", "ai_result_envelopes", "ai_result_imports", ["import_id"], ["import_id"], source_schema=SCHEMA, referent_schema=SCHEMA)
    op.create_foreign_key("fk_ai_envelopes_bundle", "ai_result_envelopes", "ai_result_bundles", ["bundle_id"], ["bundle_id"], source_schema=SCHEMA, referent_schema=SCHEMA)
    op.create_foreign_key("fk_ai_envelopes_group", "ai_result_envelopes", "ai_result_atomic_groups", ["atomic_group_id"], ["atomic_group_id"], source_schema=SCHEMA, referent_schema=SCHEMA)
    op.create_table(
        "ai_result_dependencies", _uuid("result_id", primary=True), _uuid("depends_on_result_id", primary=True),
        sa.ForeignKeyConstraint(["result_id"], [f"{SCHEMA}.ai_result_envelopes.result_id"]),
        sa.ForeignKeyConstraint(["depends_on_result_id"], [f"{SCHEMA}.ai_result_envelopes.result_id"]),
        sa.CheckConstraint("result_id <> depends_on_result_id", name="distinct_results"), schema=SCHEMA,
    )
    op.create_table(
        "watchlists", _uuid("watchlist_id", primary=True), _uuid("security_id"),
        sa.Column("state", sa.String(32), nullable=False), _uuid("latest_event_id", nullable=True),
        sa.Column("row_version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.UniqueConstraint("security_id", name="uq_watchlists_security"), schema=SCHEMA,
    )
    op.create_index("ix_watchlists_state", "watchlists", ["state", "updated_at"], schema=SCHEMA)
    op.create_table(
        "watchlist_events", _uuid("event_id", primary=True), _uuid("watchlist_id"),
        sa.Column("from_state", sa.String(32)), sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), _uuid("source_result_id", nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["watchlist_id"], [f"{SCHEMA}.watchlists.watchlist_id"]),
        sa.ForeignKeyConstraint(["source_result_id"], [f"{SCHEMA}.ai_result_envelopes.result_id"]),
        _hash_unique("watchlist_events"), schema=SCHEMA,
    )
    op.create_index("ix_watchlist_events_watchlist", "watchlist_events", ["watchlist_id", "event_time"], schema=SCHEMA)
    op.create_table(
        "watchlist_proposals", _uuid("proposal_id", primary=True), _uuid("security_id"),
        _uuid("source_result_id"), sa.Column("proposed_state", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("payload", JSON, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.ForeignKeyConstraint(["source_result_id"], [f"{SCHEMA}.ai_result_envelopes.result_id"]),
        sa.UniqueConstraint("source_result_id", name="uq_watchlist_proposals_source_result"),
        _hash_unique("watchlist_proposals"), schema=SCHEMA,
    )
    op.create_table(
        "decisions", _uuid("decision_id", primary=True), _uuid("security_id"), _uuid("task_run_id"),
        _uuid("context_pack_id"), sa.Column("context_pack_hash", sa.String(64), nullable=False),
        _uuid("source_result_id"), sa.Column("agent_identity", JSON, nullable=False),
        sa.Column("evidence_ids", JSON, nullable=False), _uuid("original_entry_plan_id", nullable=True),
        sa.Column("original_entry_plan_snapshot", JSON, nullable=False),
        sa.Column("original_entry_plan_hash", sa.String(64)),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["security_id"], [f"{SCHEMA}.securities.security_id"]),
        sa.ForeignKeyConstraint(["task_run_id"], [f"{SCHEMA}.task_runs.task_run_id"]),
        sa.ForeignKeyConstraint(["context_pack_id"], [f"{SCHEMA}.context_packs.context_pack_id"]),
        sa.ForeignKeyConstraint(["source_result_id"], [f"{SCHEMA}.ai_result_envelopes.result_id"]),
        sa.UniqueConstraint("source_result_id", name="uq_decisions_source_result"), _hash_unique("decisions"), schema=SCHEMA,
    )
    op.create_index("ix_decisions_security_as_of", "decisions", ["security_id", "as_of"], schema=SCHEMA)
    op.create_table(
        "decision_corrections", _uuid("correction_id", primary=True), _uuid("decision_id"),
        sa.Column("old_values", JSON, nullable=False), sa.Column("new_values", JSON, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("corrected_by", sa.String(128), nullable=False),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], [f"{SCHEMA}.decisions.decision_id"]),
        _hash_unique("decision_corrections"), schema=SCHEMA,
    )
    op.create_table(
        "entry_plans", _uuid("entry_plan_id", primary=True), _uuid("decision_id"),
        sa.Column("version", sa.Integer(), nullable=False), _uuid("supersedes_entry_plan_id", nullable=True),
        _uuid("created_by_review_id", nullable=True), _uuid("created_by_position_review_id", nullable=True),
        _uuid("source_result_id"), sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_horizon", sa.String(16), nullable=False), sa.Column("plan", JSON, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], [f"{SCHEMA}.decisions.decision_id"]),
        sa.ForeignKeyConstraint(["supersedes_entry_plan_id"], [f"{SCHEMA}.entry_plans.entry_plan_id"]),
        sa.ForeignKeyConstraint(["source_result_id"], [f"{SCHEMA}.ai_result_envelopes.result_id"]),
        sa.CheckConstraint("version > 0", name="positive_version"),
        sa.UniqueConstraint("decision_id", "version", name="uq_entry_plans_decision_version"),
        sa.UniqueConstraint("supersedes_entry_plan_id", name="uq_entry_plans_supersedes"),
        _hash_unique("entry_plans"), schema=SCHEMA,
    )
    op.create_index("ix_entry_plans_decision_effective", "entry_plans", ["decision_id", "effective_from"], schema=SCHEMA)
    for table, id_name, parent_name in (("reviews", "review_id", "decision_id"), ("market_reviews", "market_review_id", None)):
        columns = [_uuid(id_name, primary=True)]
        if parent_name:
            columns.extend([_uuid("decision_id"), _uuid("previous_review_id", nullable=True)])
        else:
            columns.extend([_uuid("previous_market_review_id", nullable=True), _uuid("market_regime_snapshot_id", nullable=True)])
        columns.extend([
            _uuid("task_run_id"), _uuid("context_pack_id"), sa.Column("context_pack_hash", sa.String(64), nullable=False),
            _uuid("source_result_id"), sa.Column("agent_identity", JSON, nullable=False),
            sa.Column("evidence_ids", JSON, nullable=False), sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        ])
        if table == "reviews":
            columns.extend([sa.Column("thesis_status", sa.String(32), nullable=False), sa.Column("time_efficiency", sa.String(16), nullable=False)])
            columns.extend([sa.ForeignKeyConstraint(["decision_id"], [f"{SCHEMA}.decisions.decision_id"]), sa.ForeignKeyConstraint(["previous_review_id"], [f"{SCHEMA}.reviews.review_id"])])
        else:
            columns.append(sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False))
            columns.extend([sa.ForeignKeyConstraint(["previous_market_review_id"], [f"{SCHEMA}.market_reviews.market_review_id"]), sa.ForeignKeyConstraint(["market_regime_snapshot_id"], [f"{SCHEMA}.market_regime_snapshots.regime_snapshot_id"])])
        columns.extend([
            sa.ForeignKeyConstraint(["task_run_id"], [f"{SCHEMA}.task_runs.task_run_id"]),
            sa.ForeignKeyConstraint(["context_pack_id"], [f"{SCHEMA}.context_packs.context_pack_id"]),
            sa.ForeignKeyConstraint(["source_result_id"], [f"{SCHEMA}.ai_result_envelopes.result_id"]),
            sa.UniqueConstraint("source_result_id", name=f"uq_{table}_source_result"), _hash_unique(table),
        ])
        op.create_table(table, *columns, schema=SCHEMA)
    for table in IMMUTABLE:
        op.execute(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()")


def downgrade() -> None:
    for table in reversed(IMMUTABLE):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {SCHEMA}.{table}")
    for table in ("market_reviews", "reviews", "entry_plans", "decision_corrections", "decisions", "watchlist_proposals", "watchlist_events", "watchlists", "ai_result_dependencies"):
        op.drop_table(table, schema=SCHEMA)
    for constraint in ("fk_ai_envelopes_group", "fk_ai_envelopes_bundle", "fk_ai_envelopes_import"):
        op.drop_constraint(constraint, "ai_result_envelopes", schema=SCHEMA, type_="foreignkey")
    for name in ("atomic_group_id", "bundle_id", "import_id"):
        op.drop_column("ai_result_envelopes", name, schema=SCHEMA)
    op.drop_table("ai_result_atomic_groups", schema=SCHEMA)
    op.drop_table("ai_result_bundles", schema=SCHEMA)
    op.drop_table("ai_result_imports", schema=SCHEMA)
