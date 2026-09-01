"""add index benchmark revisions for the V3 index benchmark facts

Revision ID: 0014_index_benchmark_revisions
Revises: 0013_orchestrator_job_runs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0014_index_benchmark_revisions"
down_revision = "0013_orchestrator_job_runs"
branch_labels = None
depends_on = None
SCHEMA = "v3"


def upgrade() -> None:
    op.create_table(
        "index_benchmark_revisions",
        sa.Column("revision_id", sa.Uuid(), primary_key=True),
        sa.Column("benchmark_code", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("upstream_source", sa.String(64), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "bars",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "status IN ('PUBLISHED')", name="valid_status",
        ),
        sa.UniqueConstraint("content_hash", name="uq_index_benchmark_revisions_hash"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_index_benchmark_revisions_code_time",
        "index_benchmark_revisions",
        ["benchmark_code", "known_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_index_benchmark_revisions_code_time",
        table_name="index_benchmark_revisions",
        schema=SCHEMA,
    )
    op.drop_table("index_benchmark_revisions", schema=SCHEMA)
