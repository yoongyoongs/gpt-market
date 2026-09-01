"""make trade correction replay ordered and hash chained

Revision ID: 0012_trade_correction_chain
Revises: 0011_strategy_stabilization
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0012_trade_correction_chain"
down_revision = "0011_strategy_stabilization"
branch_labels = None
depends_on = None
SCHEMA = "v3"
TABLE = "trade_corrections"


def _state_hash(state: dict[str, Any]) -> str:
    payload = json.dumps(
        state,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _state(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "fee": str(Decimal(str(row["fee"]))),
        "price": str(Decimal(str(row["price"]))),
        "quantity": str(Decimal(str(row["quantity"]))),
        "reversed": False,
        "side": row["side"],
    }


def _apply(state: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    if state["reversed"]:
        raise RuntimeError("cannot migrate correction after terminal REVERSE")
    result = dict(state)
    if row["correction_type"] == "REVERSE":
        result["reversed"] = True
        return result
    replacement = row["replacement"]
    if "side" in replacement:
        result["side"] = str(replacement["side"])
    for field in ("quantity", "price", "fee"):
        if field in replacement:
            result[field] = str(Decimal(str(replacement[field])))
    return result


def upgrade() -> None:
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_trade_corrections_immutable "
        f"ON {SCHEMA}.{TABLE}"
    )
    op.add_column(
        TABLE,
        sa.Column(
            "correction_sequence",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        TABLE,
        sa.Column("previous_effective_hash", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        TABLE,
        sa.Column("effective_hash", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.execute(
        sa.text(
            f"""
            WITH ordered AS (
                SELECT correction_id,
                       row_number() OVER (ORDER BY created_at, correction_id) AS sequence
                FROM {SCHEMA}.{TABLE}
            )
            UPDATE {SCHEMA}.{TABLE} AS correction
            SET correction_sequence = ordered.sequence
            FROM ordered
            WHERE correction.correction_id = ordered.correction_id
            """
        )
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"""
            SELECT correction.correction_id, correction.trade_id,
                   correction.correction_type, correction.replacement,
                   trade.side, trade.quantity, trade.price, trade.fee
            FROM {SCHEMA}.{TABLE} AS correction
            JOIN {SCHEMA}.trade_ledger AS trade USING (trade_id)
            ORDER BY correction.correction_sequence
            """
        )
    ).mappings()
    states: dict[Any, dict[str, Any]] = {}
    for row in rows:
        values = dict(row)
        current = states.setdefault(values["trade_id"], _state(values))
        previous_hash = _state_hash(current)
        effective = _apply(current, values)
        effective_hash = _state_hash(effective)
        bind.execute(
            sa.text(
                f"""
                UPDATE {SCHEMA}.{TABLE}
                SET previous_effective_hash = :previous_hash,
                    effective_hash = :effective_hash
                WHERE correction_id = :correction_id
                """
            ),
            {
                "previous_hash": previous_hash,
                "effective_hash": effective_hash,
                "correction_id": values["correction_id"],
            },
        )
        states[values["trade_id"]] = effective
    op.alter_column(
        TABLE, "previous_effective_hash", nullable=False, schema=SCHEMA
    )
    op.alter_column(TABLE, "effective_hash", nullable=False, schema=SCHEMA)
    op.create_unique_constraint(
        "uq_trade_corrections_sequence",
        TABLE,
        ["correction_sequence"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_trade_corrections_trade_sequence",
        TABLE,
        ["trade_id", "correction_sequence"],
        schema=SCHEMA,
    )
    op.execute(
        sa.text(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{SCHEMA}.{TABLE}', 'correction_sequence'),
                COALESCE((SELECT max(correction_sequence) FROM {SCHEMA}.{TABLE}), 1),
                EXISTS (SELECT 1 FROM {SCHEMA}.{TABLE})
            )
            """
        )
    )
    op.execute(
        f"CREATE TRIGGER trg_trade_corrections_immutable "
        f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.prevent_mutation()"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_corrections_trade_sequence", table_name=TABLE, schema=SCHEMA
    )
    op.drop_constraint(
        "uq_trade_corrections_sequence", TABLE, schema=SCHEMA, type_="unique"
    )
    op.drop_column(TABLE, "effective_hash", schema=SCHEMA)
    op.drop_column(TABLE, "previous_effective_hash", schema=SCHEMA)
    op.drop_column(TABLE, "correction_sequence", schema=SCHEMA)
