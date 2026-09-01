"""add orchestrator job runs for the V3 production pipeline

Revision ID: 0013_orchestrator_job_runs
Revises: 0012_trade_correction_chain
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0013_orchestrator_job_runs"
down_revision = "0012_trade_correction_chain"
branch_labels = None
depends_on = None
SCHEMA = "v3"


def upgrade() -> None:
    op.create_table(
        "orchestrator_job_runs",
        sa.Column("job_run_id", sa.Uuid(), primary_key=True),
        sa.Column("orchestrator_run_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "metrics",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','FAILED','SKIPPED')",
            name="valid_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="positive_attempt"),
        sa.UniqueConstraint("content_hash", name="uq_orchestrator_job_runs_hash"),
        sa.UniqueConstraint(
            "job_id", "idempotency_key", "attempt",
            name="uq_orchestrator_job_runs_job_key_attempt",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_orchestrator_job_runs_job_time",
        "orchestrator_job_runs",
        ["job_id", "known_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_orchestrator_job_runs_orchestrator_run",
        "orchestrator_job_runs",
        ["orchestrator_run_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_orchestrator_job_runs_orchestrator_run",
        table_name="orchestrator_job_runs",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_orchestrator_job_runs_job_time",
        table_name="orchestrator_job_runs",
        schema=SCHEMA,
    )
    op.drop_table("orchestrator_job_runs", schema=SCHEMA)
