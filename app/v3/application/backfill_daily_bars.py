from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from app.v3.application.aggregate_daily_bars import AggregateDailyBarsService
from app.v3.application.ingest_daily_bars import BuildDailyBarRevisionsService
from app.v3.application.publish_bar_bundle import PublishBarBundleService
from app.v3.domain.market_data import (
    BarIngestionTarget,
    BarPeriod,
    IngestionRunStatus,
    MarketDataIngestionRun,
)
from app.v3.repositories.protocols import UnitOfWork


class IngestionRunConflict(RuntimeError):
    pass


class BackfillDailyBarsService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        builder: BuildDailyBarRevisionsService,
        aggregator: AggregateDailyBarsService,
        publisher: PublishBarBundleService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._builder = builder
        self._aggregator = aggregator
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(
        self,
        *,
        run_id: UUID | None = None,
        limit: int = 300,
        minimum_last_bar_date: date,
        stop_after: int | None = None,
    ) -> MarketDataIngestionRun:
        run, targets = await self._load_or_create(run_id)
        outcomes = dict(run.cursor.get("outcomes") or {})
        if run.status is IngestionRunStatus.COMPLETED and len(outcomes) == len(targets):
            return run
        pending = [index for index in range(len(targets)) if outcomes.get(str(index), {}).get("status") != "SUCCESS"]
        attempted = 0
        run = await self._checkpoint(run, outcomes, IngestionRunStatus.RUNNING)
        for index in pending:
            if stop_after is not None and attempted >= stop_after:
                break
            target = targets[index]
            attempted += 1
            try:
                if not await self._already_published(target, minimum_last_bar_date):
                    daily = await self._builder.execute(
                        target.security_id,
                        target.code,
                        limit=limit,
                        minimum_last_bar_date=(None if target.suspended else minimum_last_bar_date),
                    )
                    aggregates = []
                    for source in filter(None, (daily.raw_revision, daily.adjusted_revision)):
                        aggregates.extend(
                            (
                                self._aggregator.execute(source, BarPeriod.WEEK),
                                self._aggregator.execute(source, BarPeriod.MONTH),
                            )
                        )
                    await self._publisher.execute(daily, aggregates)
                outcomes[str(index)] = {"status": "SUCCESS", "code": target.code}
            except Exception as exc:
                outcomes[str(index)] = {
                    "status": "FAILED",
                    "code": target.code,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            run = await self._checkpoint(run, outcomes, IngestionRunStatus.RUNNING)

        processed = len(outcomes)
        successful = sum(item.get("status") == "SUCCESS" for item in outcomes.values())
        if processed < len(targets):
            status = IngestionRunStatus.RUNNING
        elif successful == len(targets):
            status = IngestionRunStatus.COMPLETED
        elif successful == 0:
            status = IngestionRunStatus.FAILED
        else:
            status = IngestionRunStatus.PARTIAL
        return await self._checkpoint(run, outcomes, status)

    async def _load_or_create(
        self, run_id: UUID | None
    ) -> tuple[MarketDataIngestionRun, tuple[BarIngestionTarget, ...]]:
        if run_id is not None:
            async with self._uow_factory() as uow:
                run = await uow.ingestion_runs.get(run_id)
                if run is None:
                    raise ValueError(f"ingestion run {run_id} does not exist")
                targets = await uow.universes.targets(run.universe_snapshot_id)
            if len(targets) != run.expected_count:
                raise ValueError("universe target count changed for an existing run")
            return run, targets

        async with self._uow_factory() as uow:
            snapshot = await uow.universes.latest()
            if snapshot is None:
                raise ValueError("no universe snapshot is available for backfill")
            targets = await uow.universes.targets(snapshot.snapshot_id)
        now = self._clock()
        run = MarketDataIngestionRun(
            run_id=uuid4(),
            run_type="HISTORICAL_DAILY_BACKFILL",
            universe_snapshot_id=snapshot.snapshot_id,
            status=IngestionRunStatus.PENDING,
            cursor={"outcomes": {}},
            expected_count=len(targets),
            processed_count=0,
            successful_count=0,
            failed_count=0,
            started_at=now,
            row_version=1,
        )
        async with self._uow_factory() as uow:
            await uow.ingestion_runs.add(run)
            await uow.commit()
        return run, targets

    async def _already_published(
        self, target: BarIngestionTarget, minimum_last_bar_date: date
    ) -> bool:
        required_date = date(1900, 1, 1) if target.suspended else minimum_last_bar_date
        async with self._uow_factory() as uow:
            return await uow.bars.has_daily_coverage(
                target.security_id,
                minimum_bars=1,
                minimum_last_bar_date=required_date,
            )

    async def _checkpoint(
        self,
        run: MarketDataIngestionRun,
        outcomes: dict,
        status: IngestionRunStatus,
    ) -> MarketDataIngestionRun:
        successful = sum(item.get("status") == "SUCCESS" for item in outcomes.values())
        failed = sum(item.get("status") == "FAILED" for item in outcomes.values())
        errors = tuple(item for item in outcomes.values() if item.get("status") == "FAILED")
        terminal = status in {
            IngestionRunStatus.COMPLETED,
            IngestionRunStatus.PARTIAL,
            IngestionRunStatus.FAILED,
        }
        updated = run.model_copy(
            update={
                "status": status,
                "cursor": {"outcomes": outcomes},
                "processed_count": successful + failed,
                "successful_count": successful,
                "failed_count": failed,
                "errors": errors[-100:],
                "completed_at": self._clock() if terminal else None,
                "row_version": run.row_version + 1,
            }
        )
        async with self._uow_factory() as uow:
            saved = await uow.ingestion_runs.save(updated, expected_version=run.row_version)
            if not saved:
                raise IngestionRunConflict(f"run {run.run_id} was updated concurrently")
            await uow.commit()
        return updated
