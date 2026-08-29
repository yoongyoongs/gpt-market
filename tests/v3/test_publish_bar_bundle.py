from __future__ import annotations

from types import TracebackType
from uuid import uuid4

import pytest

from app.v3.application.ingest_daily_bars import BuildDailyBarRevisionsService
from app.v3.application.publish_bar_bundle import PublishBarBundleService
from tests.v3.test_ingest_daily_bars import FakeProvider, NOW


class Store:
    def __init__(self, *, fail_series: bool = False) -> None:
        self.factors = []
        self.series = []
        self.fail_series = fail_series
        self.committed = False
        self.rolled_back = False


class FakeBarRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def publish_factor_revision(self, revision):
        if revision in self.store.factors:
            return False
        self.store.factors.append(revision)
        return True

    async def publish_series_revision(self, revision):
        if self.store.fail_series:
            raise RuntimeError("series insert failed")
        if revision in self.store.series:
            return False
        self.store.series.append(revision)
        return True


class FakeUnitOfWork:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.bars = FakeBarRepository(store)

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None or not self.store.committed:
            self.store.factors.clear()
            self.store.series.clear()
            self.store.rolled_back = True

    async def commit(self) -> None:
        self.store.committed = True


async def bundle():
    return await BuildDailyBarRevisionsService(
        [FakeProvider("primary")], clock=lambda: NOW
    ).execute(uuid4(), "600000")


@pytest.mark.asyncio
async def test_factor_and_series_commit_as_one_bundle() -> None:
    store = Store()
    result = await PublishBarBundleService(lambda: FakeUnitOfWork(store)).execute(await bundle())

    assert result.factor_created is True
    assert result.series_created == 2
    assert len(store.factors) == 1
    assert len(store.series) == 2
    assert store.committed is True


@pytest.mark.asyncio
async def test_series_failure_rolls_back_factor() -> None:
    store = Store(fail_series=True)
    with pytest.raises(RuntimeError, match="series insert failed"):
        await PublishBarBundleService(lambda: FakeUnitOfWork(store)).execute(await bundle())

    assert store.factors == []
    assert store.series == []
    assert store.rolled_back is True


@pytest.mark.asyncio
async def test_republishing_same_bundle_is_idempotent() -> None:
    store = Store()
    value = await bundle()
    service = PublishBarBundleService(lambda: FakeUnitOfWork(store))

    first = await service.execute(value)
    store.committed = False
    second = await service.execute(value)

    assert first.series_created == 2
    assert second.factor_created is False
    assert second.series_created == 0
    assert len(store.factors) == 1
    assert len(store.series) == 2
