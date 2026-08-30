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

    async def latest_factor_revision_id(self, security_id):
        matching = [item for item in self.store.factors if item.security_id == security_id]
        return matching[-1].factor_revision_id if matching else None

    async def latest_series_revision_ids(self, security_id):
        latest = {}
        for item in self.store.series:
            if item.security_id == security_id:
                latest[(item.period, item.adjust_type)] = item.revision_id
        return latest

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
    assert result.series_created == 3
    assert len(store.factors) == 1
    assert len(store.series) == 3
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

    assert first.series_created == 3
    assert second.factor_created is False
    assert second.series_created == 0
    assert len(store.factors) == 1
    assert len(store.series) == 3


@pytest.mark.asyncio
async def test_new_bundle_links_previous_factor_and_series_revisions() -> None:
    store = Store()
    service = PublishBarBundleService(lambda: FakeUnitOfWork(store))
    first = await bundle()
    await service.execute(first)
    store.committed = False
    second = await BuildDailyBarRevisionsService(
        [FakeProvider("primary")], clock=lambda: NOW
    ).execute(first.adjusted_revision.security_id, "600000")

    await service.execute(second)

    assert store.factors[-1].supersedes_revision_id == store.factors[0].factor_revision_id
    previous_by_key = {(item.period, item.adjust_type): item for item in store.series[:3]}
    for revision in store.series[3:]:
        assert revision.supersedes_revision_id == previous_by_key[
            (revision.period, revision.adjust_type)
        ].revision_id
