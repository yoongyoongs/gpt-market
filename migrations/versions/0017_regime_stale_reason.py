"""regime snapshots: stale_reason (RT §23.3 stale 比例阈值 + 原因透传)

Revision ID: 0017_regime_stale_reason
Revises: 0016_attention_events
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA = "v3"

revision = "0017_regime_stale_reason"
down_revision = "0016_attention_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "market_regime_snapshots",
        sa.Column(
            "stale_reason",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("market_regime_snapshots", "stale_reason", schema=SCHEMA)
