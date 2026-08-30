from __future__ import annotations

import asyncio
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
        concurrency: int = 4,
    ) -> MarketDataIngestionRun:
        if not 1 <= concurrency <= 32:
            raise ValueError("concurrency must be between 1 and 32")
        if stop_after is not None and stop_after < 0:
            raise ValueError("stop_after cannot be negative")
        run, targets = await self._load_or_create(run_id)
        next_index, failures = self._cursor_state(run)
        if (
            run.status is IngestionRunStatus.COMPLETED
            and next_index == len(targets)
            and not failures
        ):
            return run
        covered = await self._published_coverage(targets, minimum_last_bar_date)
        pending = [*sorted(int(index) for index in failures), *range(next_index, len(targets))]
        if stop_after is not None:
            pending = pending[:stop_after]
        run = await self._checkpoint(run, next_index, failures, IngestionRunStatus.RUNNING)
        for offset in range(0, len(pending), concurrency):
            batch = pending[offset : offset + concurrency]
            results = await asyncio.gather(
                *(
                    (
                        self._skip_target(index, targets[index])
                        if targets[index].security_id in covered
                        else self._process_target(
                            index, targets[index], limit, minimum_last_bar_date
                        )
                    )
                    for index in batch
                )
            )
            for index, outcome in results:
                if outcome["status"] == "SUCCESS":
                    failures.pop(str(index), None)
                else:
                    failures[str(index)] = outcome
            new_indices = [index for index in batch if index >= next_index]
            if new_indices:
                if new_indices != list(range(next_index, next_index + len(new_indices))):
                    raise RuntimeError("backfill high-water mark lost target ordering")
                next_index += len(new_indices)
            run = await self._checkpoint(
                run, next_index, failures, IngestionRunStatus.RUNNING
            )

        processed = next_index
        successful = processed - len(failures)
        if processed < len(targets):
            status = IngestionRunStatus.RUNNING
        elif successful == len(targets):
            status = IngestionRunStatus.COMPLETED
        elif successful == 0:
            status = IngestionRunStatus.FAILED
        else:
            status = IngestionRunStatus.PARTIAL
        return await self._checkpoint(run, next_index, failures, status)

    @staticmethod
    def _cursor_state(
        run: MarketDataIngestionRun,
    ) -> tuple[int, dict[str, dict[str, str]]]:
        if "next_index" in run.cursor:
            return int(run.cursor["next_index"]), dict(run.cursor.get("failures") or {})
        outcomes = dict(run.cursor.get("outcomes") or {})
        next_index = 0
        while str(next_index) in outcomes:
            next_index += 1
        failures = {
            index: outcome
            for index, outcome in outcomes.items()
            if int(index) < next_index and outcome.get("status") == "FAILED"
        }
        return next_index, failures

    async def _process_target(
        self,
        index: int,
        target: BarIngestionTarget,
        limit: int,
        minimum_last_bar_date: date,
    ) -> tuple[int, dict[str, str]]:
        try:
            daily = await self._builder.execute(
                target.security_id,
                target.code,
                limit=limit,
                minimum_last_bar_date=(None if target.suspended else minimum_last_bar_date),
            )
            aggregates = []
            for source in filter(
                None, (daily.raw_revision, daily.adjusted_revision, daily.hfq_revision)
            ):
                aggregates.extend(
                    (
                        self._aggregator.execute(source, BarPeriod.WEEK),
                        self._aggregator.execute(source, BarPeriod.MONTH),
                    )
                )
            await self._publisher.execute(daily, aggregates)
            return index, {"status": "SUCCESS", "code": target.code}
        except Exception as exc:
            return index, {
                "status": "FAILED",
                "code": target.code,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }

    @staticmethod
    async def _skip_target(
        index: int, target: BarIngestionTarget
    ) -> tuple[int, dict[str, str]]:
        return index, {"status": "SUCCESS", "code": target.code, "coverage_skip": "true"}

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
            cursor={"next_index": 0, "failures": {}},
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

    async def _published_coverage(
        self,
        targets: tuple[BarIngestionTarget, ...],
        minimum_last_bar_date: date,
    ) -> set[UUID]:
        async with self._uow_factory() as uow:
            return await uow.bars.covered_daily_security_ids(
                targets,
                minimum_bars=1,
                minimum_last_bar_date=minimum_last_bar_date,
            )

    async def _checkpoint(
        self,
        run: MarketDataIngestionRun,
        next_index: int,
        failures: dict[str, dict[str, str]],
        status: IngestionRunStatus,
    ) -> MarketDataIngestionRun:
        failed = len(failures)
        successful = next_index - failed
        errors = tuple(failures[index] for index in sorted(failures, key=int))
        terminal = status in {
            IngestionRunStatus.COMPLETED,
            IngestionRunStatus.PARTIAL,
            IngestionRunStatus.FAILED,
        }
        updated = run.model_copy(
            update={
                "status": status,
                "cursor": {"next_index": next_index, "failures": failures},
                "processed_count": next_index,
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
