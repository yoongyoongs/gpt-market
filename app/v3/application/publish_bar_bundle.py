from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.v3.application.aggregate_daily_bars import LocalAggregationResult
from app.v3.application.ingest_daily_bars import DailyBarIngestionBundle
from app.v3.domain.market_data import (
    AdjustmentFactorRevision,
    AdjustmentFactorRevisionContent,
    BarSeriesRevision,
    BarSeriesRevisionContent,
)
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
            revisions = tuple(
                revision
                for revision in (
                    daily.raw_revision,
                    daily.adjusted_revision,
                    daily.hfq_revision,
                    *(aggregate.revision for aggregate in aggregates),
                )
                if revision is not None
            )
            security_id = daily.adjusted_revision.security_id
            previous_factor_id = await uow.bars.latest_factor_revision_id(security_id)
            previous_series_ids = await uow.bars.latest_series_revision_ids(security_id)
            if daily.factor_revision is not None:
                factor = self._factor_with_supersedes(
                    daily.factor_revision, previous_factor_id
                )
                factor_created = await uow.bars.publish_factor_revision(factor)
            for revision in revisions:
                linked = self._series_with_supersedes(
                    revision,
                    previous_series_ids.get((revision.period, revision.adjust_type)),
                )
                series_created += int(await uow.bars.publish_series_revision(linked))
            await uow.commit()
        return PublishBarBundleResult(factor_created, series_created)

    @staticmethod
    def _factor_with_supersedes(
        revision: AdjustmentFactorRevision, previous_id
    ) -> AdjustmentFactorRevision:
        if previous_id is None or previous_id == revision.factor_revision_id:
            return revision
        content = AdjustmentFactorRevisionContent.model_validate(
            revision.model_dump(exclude={"content_hash"})
        ).model_copy(update={"supersedes_revision_id": previous_id})
        return AdjustmentFactorRevision.build(content)

    @staticmethod
    def _series_with_supersedes(
        revision: BarSeriesRevision, previous_id
    ) -> BarSeriesRevision:
        if previous_id is None or previous_id == revision.revision_id:
            return revision
        content = BarSeriesRevisionContent.model_validate(
            revision.model_dump(exclude={"content_hash"})
        ).model_copy(update={"supersedes_revision_id": previous_id})
        return BarSeriesRevision.build(content)
