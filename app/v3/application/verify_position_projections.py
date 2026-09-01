from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.v3.domain.strategy import OperationalHealthEventCreate

# 比对的 Projection 数值维度；任何一维不一致都视为漂移
_COMPARED_FIELDS = ("quantity", "cost_basis", "average_cost", "cash_impact", "realized_pnl")
# Projection 列为 Numeric(24, 6)：存储值是重放值按列精度取整的结果，
# 差异在半个最小单位（0.5e-6）内属于合法取整，不算漂移。
_PROJECTION_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class ProjectionVerifyReport:
    checked: int
    mismatches: list[dict] = field(default_factory=list)
    events_written: int = 0


class VerifyPositionProjectionsService:
    """Projection Verify Job（POR-005）：

    用 Opening + Ledger + Corrections + CONFIRMED Adjustments 全量重放，
    与存储的 Position Projection 比对；漂移时写 OperationalHealthEvent
    报警，绝不静默修复 Ledger 或 Projection。
    """

    def __init__(self, uow_factory: Callable) -> None:
        self._uow_factory = uow_factory

    async def execute(self) -> ProjectionVerifyReport:
        mismatches: list[dict] = []
        events_written = 0
        async with self._uow_factory() as uow:
            keys = await uow.portfolios.projection_keys()
            for account_id, security_id in keys:
                stored = await uow.portfolios.position(account_id, security_id)
                replayed = await uow.portfolios.replay_position_values(
                    account_id, security_id
                )
                differences = self._compare(stored, replayed)
                if not differences:
                    continue
                mismatch = {
                    "account_id": str(account_id),
                    "security_id": str(security_id),
                    "differences": differences,
                }
                mismatches.append(mismatch)
                await uow.strategies.add_health_event(
                    OperationalHealthEventCreate(
                        health_event_id=uuid4(),
                        component="v3.portfolio",
                        capability="projection_verify",
                        status="FAILED",
                        error_type="PROJECTION_DRIFT",
                        circuit_state="CLOSED",
                        observed_at=datetime.now(timezone.utc),
                        metadata=mismatch,
                    )
                )
                events_written += 1
            await uow.commit()
        return ProjectionVerifyReport(
            checked=len(keys), mismatches=mismatches, events_written=events_written
        )

    @staticmethod
    def _compare(
        stored: dict, replayed: dict[str, Decimal]
    ) -> dict[str, dict[str, str]]:
        differences: dict[str, dict[str, str]] = {}
        for field_name in _COMPARED_FIELDS:
            stored_value = stored.get(field_name)
            replayed_value = replayed[field_name]
            if stored_value is None or abs(
                Decimal(stored_value) - replayed_value
            ) > _PROJECTION_QUANTUM / 2:
                differences[field_name] = {
                    "stored": "UNKNOWN" if stored_value is None else str(stored_value),
                    "replayed": str(replayed_value),
                }
        return differences
