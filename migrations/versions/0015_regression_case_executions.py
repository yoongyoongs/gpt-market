"""add regression case executions for the V3 regression case real execution

Revision ID: 0015_regression_case_executions
Revises: 0014_index_benchmark_revisions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0015_regression_case_executions"
down_revision = "0014_index_benchmark_revisions"
branch_labels = None
depends_on = None
SCHEMA = "v3"


def upgrade() -> None:
    op.create_table(
        "regression_case_executions",
        sa.Column("execution_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "regression_case_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.regression_cases.regression_case_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "replay_run_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.replay_runs.replay_run_id"),
        ),
        sa.Column("blocked_reason", sa.Text()),
        sa.Column(
            "invariant_results",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "diff",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "status IN ('PASS', 'FAIL', 'BLOCKED')", name="valid_execution_status",
        ),
        sa.UniqueConstraint("content_hash", name="uq_regression_case_executions_hash"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_regression_case_executions_case_time",
        "regression_case_executions",
        ["regression_case_id", "known_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_regression_case_executions_case_time",
        table_name="regression_case_executions",
        schema=SCHEMA,
    )
    op.drop_table("regression_case_executions", schema=SCHEMA)
