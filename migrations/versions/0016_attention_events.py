"""add attention events for the V3 trigger / attention engine

Revision ID: 0016_attention_events
Revises: 0015_regression_case_executions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

SCHEMA = "v3"

revision = "0016_attention_events"
down_revision = "0015_regression_case_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attention_events",
        sa.Column("attention_event_id", sa.Uuid(), primary_key=True),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=True),
        sa.Column("code", sa.String(16), nullable=True),
        sa.Column("market", sa.String(8), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("entry_plan_id", sa.Uuid(), nullable=True),
        sa.Column("position_review_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column(
            "facts",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_snapshot_ids",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("dedupe_key", sa.String(256), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "status IN ('OPEN', 'ACKED', 'RESOLVED', 'EXPIRED')",
            name="valid_attention_status",
        ),
        sa.CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'CRITICAL')",
            name="valid_attention_severity",
        ),
        sa.UniqueConstraint("content_hash", name="uq_attention_events_hash"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_attention_events_dedupe_time",
        "attention_events",
        ["dedupe_key", "known_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_attention_events_status_time",
        "attention_events",
        ["status", "known_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_attention_events_status_time", table_name="attention_events", schema=SCHEMA,
    )
    op.drop_index(
        "ix_attention_events_dedupe_time", table_name="attention_events", schema=SCHEMA,
    )
    op.drop_table("attention_events", schema=SCHEMA)
