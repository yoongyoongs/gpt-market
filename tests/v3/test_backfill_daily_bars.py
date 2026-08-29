from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import TracebackType
from uuid import uuid4

import pytest

from app.v3.application.aggregate_daily_bars import AggregateDailyBarsService
from app.v3.application.backfill_daily_bars import BackfillDailyBarsService, IngestionRunConflict
from app.v3.application.ingest_daily_bars import BuildDailyBarRevisionsService
from app.v3.domain.market_data import (
    BarIngestionTarget,
    IngestionRunStatus,
    Market,
)
from tests.v3.test_ingest_daily_bars import FakeProvider, NOW, result
from tests.v3.test_refresh_universe import snapshot


class AlwaysOpenCalendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5


class DynamicProvider(FakeProvider):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.failed_codes: set[str] = set()

    async def fetch(self, code, period, adjust_type, limit):
        if code in self.failed_codes and adjust_type.value == "QFQ":
            raise RuntimeError(f"{code} unavailable")
        return result(self.code, adjust_type).model_copy(update={"code": code})


class TrackingProvider(DynamicProvider):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.active = 0
        self.maximum_active = 0

    async def fetch(self, code, period, adjust_type, limit):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.005)
            return await super().fetch(code, period, adjust_type, limit)
        finally:
            self.active -= 1


class Store:
    def __init__(self, targets) -> None:
        self.snapshot = snapshot(len(targets))
        self.targets = tuple(targets)
        self.runs = {}
        self.coverage = set()
        self.save_calls = 0
        self.fail_save_call: int | None = None


class FakeUniverseRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def latest(self):
        return self.store.snapshot

    async def targets(self, snapshot_id):
        assert snapshot_id == self.store.snapshot.snapshot_id
        return self.store.targets


class FakeBarRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def has_daily_coverage(self, security_id, **kwargs):
        return security_id in self.store.coverage


class FakeRunRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def add(self, run):
        self.store.runs[run.run_id] = run

    async def get(self, run_id):
        return self.store.runs.get(run_id)

    async def save(self, run, *, expected_version):
        self.store.save_calls += 1
        if self.store.fail_save_call == self.store.save_calls:
            return False
        current = self.store.runs[run.run_id]
        if current.row_version != expected_version:
            return False
        self.store.runs[run.run_id] = run
        return True


class FakeUnitOfWork:
    def __init__(self, store: Store) -> None:
        self.universes = FakeUniverseRepository(store)
        self.bars = FakeBarRepository(store)
        self.ingestion_runs = FakeRunRepository(store)

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self):
        return None


class FakePublisher:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls = 0

    async def execute(self, daily, aggregates=()):
        self.calls += 1
        self.store.coverage.add(daily.adjusted_revision.security_id)


def target(code: str) -> BarIngestionTarget:
    return BarIngestionTarget(security_id=uuid4(), code=code, market=Market.SH)


def service(store: Store, provider: DynamicProvider, publisher: FakePublisher):
    return BackfillDailyBarsService(
        lambda: FakeUnitOfWork(store),
        BuildDailyBarRevisionsService([provider], clock=lambda: NOW),
        AggregateDailyBarsService(AlwaysOpenCalendar(), clock=lambda: NOW),
        publisher,
        clock=lambda: datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_partial_run_retries_only_failed_targets() -> None:
    targets = (target("600000"), target("600001"))
    store = Store(targets)
    provider = DynamicProvider("provider")
    provider.failed_codes.add("600001")
    publisher = FakePublisher(store)
    runner = service(store, provider, publisher)

    first = await runner.execute(minimum_last_bar_date=date(2026, 8, 20))
    assert first.status is IngestionRunStatus.PARTIAL
    assert (first.processed_count, first.successful_count, first.failed_count) == (2, 1, 1)

    calls_before_resume = publisher.calls
    provider.failed_codes.clear()
    resumed = await runner.execute(
        run_id=first.run_id,
        minimum_last_bar_date=date(2026, 8, 20),
    )
    assert resumed.status is IngestionRunStatus.COMPLETED
    assert (resumed.processed_count, resumed.successful_count, resumed.failed_count) == (2, 2, 0)
    assert publisher.calls == calls_before_resume + 1


@pytest.mark.asyncio
async def test_crash_after_publish_resumes_without_duplicate_revision() -> None:
    targets = (target("600000"),)
    store = Store(targets)
    store.fail_save_call = 2
    provider = DynamicProvider("provider")
    publisher = FakePublisher(store)
    runner = service(store, provider, publisher)

    with pytest.raises(IngestionRunConflict):
        await runner.execute(minimum_last_bar_date=date(2026, 8, 20))
    run_id = next(iter(store.runs))
    assert publisher.calls == 1

    store.fail_save_call = None
    resumed = await runner.execute(
        run_id=run_id,
        minimum_last_bar_date=date(2026, 8, 20),
    )
    assert resumed.status is IngestionRunStatus.COMPLETED
    assert publisher.calls == 1


@pytest.mark.asyncio
async def test_backfill_bounds_concurrency_and_checkpoints_every_batch() -> None:
    targets = tuple(target(f"6{index:05d}") for index in range(6))
    store = Store(targets)
    provider = TrackingProvider("provider")
    publisher = FakePublisher(store)

    completed = await service(store, provider, publisher).execute(
        minimum_last_bar_date=date(2026, 8, 20),
        concurrency=3,
    )

    assert completed.status is IngestionRunStatus.COMPLETED
    assert completed.processed_count == 6
    assert provider.maximum_active == 3
    assert store.save_calls == 4
    assert completed.cursor == {"next_index": 6, "failures": {}}


@pytest.mark.asyncio
async def test_backfill_rejects_unbounded_concurrency() -> None:
    store = Store((target("600000"),))
    runner = service(store, DynamicProvider("provider"), FakePublisher(store))

    with pytest.raises(ValueError, match="between 1 and 32"):
        await runner.execute(
            minimum_last_bar_date=date(2026, 8, 20),
            concurrency=33,
        )
