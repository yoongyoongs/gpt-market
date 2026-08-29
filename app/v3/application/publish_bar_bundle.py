from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.v3.application.aggregate_daily_bars import LocalAggregationResult
from app.v3.application.ingest_daily_bars import DailyBarIngestionBundle
from app.v3.repositories.protocols import UnitOfWork


@dataclass(frozen=True)
class PublishBarBundleResult:
    factor_created: bool
    series_created: int


class PublishBarBundleService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self,
        daily: DailyBarIngestionBundle,
        aggregates: Sequence[LocalAggregationResult] = (),
    ) -> PublishBarBundleResult:
        factor_created = False
        series_created = 0
        async with self._uow_factory() as uow:
            if daily.factor_revision is not None:
                factor_created = await uow.bars.publish_factor_revision(daily.factor_revision)
            if daily.raw_revision is not None:
                series_created += int(await uow.bars.publish_series_revision(daily.raw_revision))
            series_created += int(await uow.bars.publish_series_revision(daily.adjusted_revision))
            for aggregate in aggregates:
                if aggregate.revision is not None:
                    series_created += int(await uow.bars.publish_series_revision(aggregate.revision))
            await uow.commit()
        return PublishBarBundleResult(factor_created, series_created)
