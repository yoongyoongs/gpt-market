"""create V3 Phase 11 strategy shadow and stabilization

Revision ID: 0011_strategy_stabilization
Revises: 0010_performance_replay
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_strategy_stabilization"
down_revision = "0010_performance_replay"
branch_labels = None
depends_on = None
SCHEMA = "v3"
JSON = postgresql.JSONB(astext_type=sa.Text())
IMMUTABLE = (
    "strategy_versions", "strategy_proposals", "guardrail_versions",
    "strategy_experiments", "strategy_experiment_events", "shadow_observations",
    "capacity_evaluations", "release_events", "operational_health_events",
)


def u(name, *, pk=False, nullable=False):
    return sa.Column(name, sa.Uuid(), primary_key=pk, nullable=False if pk else nullable)


def upgrade():
    op.create_table("strategy_versions", u("strategy_version_id", pk=True),
        sa.Column("strategy_code", sa.String(64), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        u("supersedes_strategy_version_id", nullable=True), sa.Column("configuration", JSON, nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False), sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True)), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["supersedes_strategy_version_id"], [f"{SCHEMA}.strategy_versions.strategy_version_id"]),
        sa.CheckConstraint("version > 0", name="positive_version"),
        sa.UniqueConstraint("strategy_code", "version", name="uq_strategy_versions_code_version"),
        sa.UniqueConstraint("supersedes_strategy_version_id", name="uq_strategy_versions_supersedes"),
        sa.UniqueConstraint("content_hash", name="uq_strategy_versions_hash"), schema=SCHEMA)
    op.create_table("strategy_proposals", u("proposal_id", pk=True), u("proposed_strategy_version_id"),
        sa.Column("actor_type", sa.String(16), nullable=False), sa.Column("actor_id", sa.String(128), nullable=False),
        u("source_result_id", nullable=True), sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("expected_improvements", JSON, nullable=False), sa.Column("risks", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["proposed_strategy_version_id"], [f"{SCHEMA}.strategy_versions.strategy_version_id"]),
        sa.ForeignKeyConstraint(["source_result_id"], [f"{SCHEMA}.ai_result_envelopes.result_id"]),
        sa.CheckConstraint("actor_type IN ('AI','HUMAN','SYSTEM')", name="valid_actor_type"),
        sa.UniqueConstraint("content_hash", name="uq_strategy_proposals_hash"), schema=SCHEMA)
    op.create_table("guardrail_versions", u("guardrail_version_id", pk=True),
        sa.Column("guardrail_code", sa.String(64), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        u("supersedes_guardrail_version_id", nullable=True),
        sa.Column("max_error_rate", sa.Numeric(8, 7), nullable=False),
        sa.Column("max_p95_ms", sa.Numeric(12, 3), nullable=False),
        sa.Column("min_shadow_sample_count", sa.Integer(), nullable=False),
        sa.Column("max_divergence_rate", sa.Numeric(8, 7), nullable=False),
        sa.Column("max_capacity_utilization", sa.Numeric(8, 7), nullable=False),
        sa.Column("rollback_on_provider_failure", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["supersedes_guardrail_version_id"], [f"{SCHEMA}.guardrail_versions.guardrail_version_id"]),
        sa.CheckConstraint("version > 0 AND min_shadow_sample_count > 0", name="positive_version_samples"),
        sa.CheckConstraint("max_error_rate BETWEEN 0 AND 1 AND max_divergence_rate BETWEEN 0 AND 1 AND max_capacity_utilization > 0 AND max_capacity_utilization <= 1", name="valid_thresholds"),
        sa.UniqueConstraint("guardrail_code", "version", name="uq_guardrail_versions_code_version"),
        sa.UniqueConstraint("supersedes_guardrail_version_id", name="uq_guardrail_versions_supersedes"),
        sa.UniqueConstraint("content_hash", name="uq_guardrail_versions_hash"), schema=SCHEMA)
    op.create_table("strategy_experiments", u("experiment_id", pk=True),
        sa.Column("experiment_type", sa.String(16), nullable=False), u("control_strategy_version_id", nullable=True),
        u("treatment_strategy_version_id"), u("guardrail_version_id"),
        sa.Column("allocation_percent", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False), sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(128), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["control_strategy_version_id"], [f"{SCHEMA}.strategy_versions.strategy_version_id"]),
        sa.ForeignKeyConstraint(["treatment_strategy_version_id"], [f"{SCHEMA}.strategy_versions.strategy_version_id"]),
        sa.ForeignKeyConstraint(["guardrail_version_id"], [f"{SCHEMA}.guardrail_versions.guardrail_version_id"]),
        sa.CheckConstraint("experiment_type IN ('SHADOW','AB')", name="valid_type"),
        sa.CheckConstraint("allocation_percent BETWEEN 0 AND 100", name="valid_allocation"),
        sa.CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="valid_window"),
        sa.CheckConstraint("(experiment_type = 'SHADOW' AND allocation_percent = 0) OR (experiment_type = 'AB' AND control_strategy_version_id IS NOT NULL AND allocation_percent BETWEEN 1 AND 99)", name="valid_mode_allocation"),
        sa.UniqueConstraint("content_hash", name="uq_strategy_experiments_hash"), schema=SCHEMA)
    op.create_table("strategy_experiment_events", u("event_id", pk=True), u("experiment_id"),
        sa.Column("sequence", sa.Integer(), nullable=False), sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False), sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], [f"{SCHEMA}.strategy_experiments.experiment_id"]),
        sa.CheckConstraint("sequence > 0", name="positive_sequence"),
        sa.UniqueConstraint("experiment_id", "sequence", name="uq_strategy_experiment_events_sequence"),
        sa.UniqueConstraint("content_hash", name="uq_strategy_experiment_events_hash"), schema=SCHEMA)
    op.create_table("shadow_observations", u("shadow_observation_id", pk=True), u("experiment_id"),
        sa.Column("subject_key", sa.String(256), nullable=False), sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("control_output_hash", sa.String(64), nullable=False), sa.Column("treatment_output_hash", sa.String(64), nullable=False),
        sa.Column("control_payload", JSON, nullable=False), sa.Column("treatment_payload", JSON, nullable=False),
        sa.Column("materially_divergent", sa.Boolean(), nullable=False), sa.Column("divergence_reason", sa.Text()),
        sa.Column("latency_ms", sa.Numeric(12, 3), nullable=False), sa.Column("error", sa.Text()),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], [f"{SCHEMA}.strategy_experiments.experiment_id"]),
        sa.CheckConstraint("latency_ms >= 0", name="nonnegative_latency"),
        sa.UniqueConstraint("experiment_id", "subject_key", "observed_at", name="uq_shadow_observations_subject_time"),
        sa.UniqueConstraint("content_hash", name="uq_shadow_observations_hash"), schema=SCHEMA)
    op.create_index("ix_shadow_observations_experiment_time", "shadow_observations", ["experiment_id", "observed_at"], schema=SCHEMA)
    op.create_table("capacity_evaluations", u("capacity_evaluation_id", pk=True),
        u("strategy_version_id"), u("guardrail_version_id"),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False), sa.Column("error_rate", sa.Numeric(8, 7), nullable=False),
        sa.Column("p95_ms", sa.Numeric(12, 3), nullable=False), sa.Column("divergence_rate", sa.Numeric(8, 7), nullable=False),
        sa.Column("capacity_utilization", sa.Numeric(8, 7), nullable=False), sa.Column("provider_failures", sa.Integer(), nullable=False),
        sa.Column("metrics", JSON, nullable=False), sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("failures", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["strategy_version_id"], [f"{SCHEMA}.strategy_versions.strategy_version_id"]),
        sa.ForeignKeyConstraint(["guardrail_version_id"], [f"{SCHEMA}.guardrail_versions.guardrail_version_id"]),
        sa.CheckConstraint("sample_count > 0 AND error_rate BETWEEN 0 AND 1 AND divergence_rate BETWEEN 0 AND 1 AND capacity_utilization >= 0 AND provider_failures >= 0", name="valid_metrics"),
        sa.UniqueConstraint("content_hash", name="uq_capacity_evaluations_hash"), schema=SCHEMA)
    op.create_index("ix_capacity_evaluations_strategy_time", "capacity_evaluations", ["strategy_version_id", "evaluated_at"], schema=SCHEMA)
    op.create_table("release_states", u("release_state_id", pk=True),
        sa.Column("environment", sa.String(32), nullable=False), sa.Column("mode", sa.String(16), nullable=False),
        u("active_strategy_version_id", nullable=True), u("active_guardrail_version_id", nullable=True),
        sa.Column("row_version", sa.BigInteger(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["active_strategy_version_id"], [f"{SCHEMA}.strategy_versions.strategy_version_id"]),
        sa.ForeignKeyConstraint(["active_guardrail_version_id"], [f"{SCHEMA}.guardrail_versions.guardrail_version_id"]),
        sa.CheckConstraint("mode IN ('V2','SHADOW','AB','V3')", name="valid_mode"),
        sa.CheckConstraint("row_version >= 0", name="nonnegative_row_version"),
        sa.CheckConstraint("(mode = 'V2' AND active_strategy_version_id IS NULL AND active_guardrail_version_id IS NULL) OR (mode <> 'V2' AND active_strategy_version_id IS NOT NULL AND active_guardrail_version_id IS NOT NULL)", name="valid_active_binding"),
        sa.UniqueConstraint("environment", name="uq_release_states_environment"), schema=SCHEMA)
    op.create_table("release_events", u("release_event_id", pk=True),
        sa.Column("environment", sa.String(32), nullable=False), sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("from_mode", sa.String(16), nullable=False), sa.Column("to_mode", sa.String(16), nullable=False),
        u("proposal_id", nullable=True), u("strategy_version_id", nullable=True), u("guardrail_version_id", nullable=True),
        sa.Column("actor_type", sa.String(16), nullable=False), sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("gate_snapshot", JSON, nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["proposal_id"], [f"{SCHEMA}.strategy_proposals.proposal_id"]),
        sa.ForeignKeyConstraint(["strategy_version_id"], [f"{SCHEMA}.strategy_versions.strategy_version_id"]),
        sa.ForeignKeyConstraint(["guardrail_version_id"], [f"{SCHEMA}.guardrail_versions.guardrail_version_id"]),
        sa.CheckConstraint("from_mode IN ('V2','SHADOW','AB','V3') AND to_mode IN ('V2','SHADOW','AB','V3')", name="valid_modes"),
        sa.CheckConstraint("actor_type IN ('HUMAN','SYSTEM')", name="human_or_system_actor"),
        sa.UniqueConstraint("environment", "sequence", name="uq_release_events_environment_sequence"),
        sa.UniqueConstraint("content_hash", name="uq_release_events_hash"), schema=SCHEMA)
    op.create_table("operational_health_events", u("health_event_id", pk=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("component", sa.String(128), nullable=False), sa.Column("capability", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("latency_ms", sa.Numeric(12, 3)),
        sa.Column("error_type", sa.String(128)), sa.Column("circuit_state", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_payload", JSON, nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("status IN ('HEALTHY','DEGRADED','FAILED','UNKNOWN')", name="valid_status"),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="nonnegative_latency"),
        sa.UniqueConstraint("content_hash", name="uq_operational_health_events_hash"), schema=SCHEMA)
    op.create_index("ix_operational_health_events_component_time", "operational_health_events", ["component", "observed_at"], schema=SCHEMA)
    for table in IMMUTABLE:
        op.execute(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()")


def downgrade():
    for table in reversed(IMMUTABLE):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {SCHEMA}.{table}")
    for table in ("operational_health_events", "release_events", "release_states", "capacity_evaluations", "shadow_observations", "strategy_experiment_events", "strategy_experiments", "guardrail_versions", "strategy_proposals", "strategy_versions"):
        op.drop_table(table, schema=SCHEMA)
