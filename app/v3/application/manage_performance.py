from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.performance import (
    PerformanceAbility,
    PerformanceAttributionCreate,
    RegressionCaseCreate,
    ReplayRunCreate,
)


class PerformanceSummaryCommand(V3Contract):
    ability: PerformanceAbility
    regime_snapshot_id: UUID | None = None
    strategy_version: str = Field(min_length=1, max_length=64)
    window_start: datetime
    window_end: datetime

    @field_validator("window_start", "window_end")
    @classmethod
    def validate_times(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)


class RecallMissSnapshotCommand(V3Contract):
    threshold_version: str = Field(min_length=1, max_length=64)


class PerformanceService:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def add_attribution(self, command: PerformanceAttributionCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.performance.add_attribution(command)
            await uow.commit()
        return {"attribution_id": object_id, "ability": command.ability}

    async def replay(self, command: ReplayRunCreate):
        async with self._uow_factory() as uow:
            result = await uow.performance.run_replay(command)
            await uow.commit()
        return result

    async def add_regression_case(self, command: RegressionCaseCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.performance.add_regression_case(command)
            await uow.commit()
        return {"regression_case_id": object_id, "status": "RECORDED"}

    async def summarize(self, command: PerformanceSummaryCommand):
        if command.window_end < command.window_start:
            raise ValueError("window_end cannot be earlier than window_start")
        async with self._uow_factory() as uow:
            result = await uow.performance.summarize(
                command.ability.value, command.regime_snapshot_id,
                command.strategy_version, command.window_start, command.window_end,
            )
            await uow.commit()
        return result

    async def snapshot_recall_misses(self, command: RecallMissSnapshotCommand):
        async with self._uow_factory() as uow:
            result = await uow.performance.snapshot_recall_misses(
                command.threshold_version
            )
            await uow.commit()
        return result
