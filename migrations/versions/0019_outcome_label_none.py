"""outcome_labels: 成熟负样本显式 NONE（R2.1-P0-03）

status=MATURED 且不达标 → label='NONE'；NULL 仅代表 PENDING。
旧约束 label IN ('A','B','C') 拒收 'NONE'，本版扩到四值。

Revision ID: 0019_outcome_label_none
Revises: 0018_candidate_scan_tables
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

SCHEMA = "v3"

revision = "0019_outcome_label_none"
down_revision = "0018_candidate_scan_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("valid_label", "outcome_labels", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "valid_label",
        "outcome_labels",
        "label IS NULL OR label IN ('A','B','C','NONE')",
        schema=SCHEMA,
    )


def downgrade() -> None:
    # 回滚前需清掉存量 'NONE' 行（成熟负样本在旧语义下即 NULL）
    op.execute(f"UPDATE {SCHEMA}.outcome_labels SET label = NULL WHERE label = 'NONE'")
    op.drop_constraint("valid_label", "outcome_labels", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "valid_label",
        "outcome_labels",
        "label IS NULL OR label IN ('A','B','C')",
        schema=SCHEMA,
    )
