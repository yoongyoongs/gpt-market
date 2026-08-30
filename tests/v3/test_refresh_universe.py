from __future__ import annotations

from datetime import datetime, timezone
from types import TracebackType
from uuid import uuid4

import pytest

from app.v3.application.refresh_universe import (
    AllUniverseProvidersFailed,
    RefreshUniverseService,
)
from app.v3.domain.market_data import (
    Market,
    SecurityMember,
    UniverseFetchResult,
    UniverseSnapshot,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)


NOW = datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc)


def members(count: int) -> tuple[SecurityMember, ...]:
    return tuple(
        SecurityMember(
            code=f"{index:06d}",
            market=Market.SH if index % 2 else Market.SZ,
            name=f"股票{index}",
        )
        for index in range(1, count + 1)
    )


def fetched(source: str, count: int, expected: int | None = None) -> UniverseFetchResult:
    return UniverseFetchResult(
        source_code=source,
        as_of=NOW,
        fetch_time=NOW,
        expected_total=expected or count,
        members=members(count),
    )


def snapshot(count: int = 100) -> UniverseSnapshot:
    return UniverseSnapshot.build(
        UniverseSnapshotContent(
            snapshot_id=uuid4(),
            source_code="eastmoney",
            status=UniverseSnapshotStatus.PRIMARY,
            as_of=NOW,
            fetch_time=NOW,
            known_at=NOW,
            coverage=1,
            stale=False,
            members=members(count),
        )
    )


class FakeProvider:
    def __init__(self, code: str, result=None, error: Exception | None = None) -> None:
        self.code = code
        self.result = result
        self.error = error
        self.calls = 0

    async def fetch_snapshot(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def close(self) -> None:
        return None


class Store:
    def __init__(
        self, latest: UniverseSnapshot | None = None, *, publish_error: Exception | None = None
    ) -> None:
        self.latest = latest
        self.published: list[UniverseSnapshot] = []
        self.publish_error = publish_error


class FakeUniverseRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def latest(self):
        return self.store.latest

    async def publish(self, value):
        if self.store.publish_error:
            raise self.store.publish_error
        self.store.published.append(value)
        self.store.latest = value
        return True


class FakeUnitOfWork:
    def __init__(self, store: Store) -> None:
        self.universes = FakeUniverseRepository(store)
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def service(store: Store, *providers: FakeProvider) -> RefreshUniverseService:
    return RefreshUniverseService(
        lambda: FakeUnitOfWork(store),
        providers,
        minimum_members=90,
        minimum_coverage=0.9,
        minimum_retention=0.9,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_primary_failure_is_taken_over_by_secondary() -> None:
    store = Store(snapshot())
    result = await service(
        store,
        FakeProvider("primary", error=RuntimeError("down")),
        FakeProvider("secondary", result=fetched("secondary", 100)),
    ).execute()

    assert result.snapshot.status is UniverseSnapshotStatus.SECONDARY
    assert result.snapshot.stale is False
    assert result.snapshot.previous_snapshot_id is not None
    assert result.provider_errors[0].startswith("primary: RuntimeError")


@pytest.mark.asyncio
async def test_coverage_drop_is_rejected_and_falls_back_to_lkg() -> None:
    previous = snapshot()
    store = Store(previous)
    result = await service(
        store,
        FakeProvider("primary", result=fetched("primary", 89, 100)),
        FakeProvider("secondary", error=RuntimeError("down")),
    ).execute()

    assert result.snapshot.status is UniverseSnapshotStatus.LKG
    assert result.snapshot.stale is True
    assert result.snapshot.members == previous.members
    assert "UniverseCoverageError" in result.provider_errors[0]


@pytest.mark.asyncio
async def test_unexpected_universe_expansion_is_rejected() -> None:
    previous = snapshot()
    store = Store(previous)
    result = await service(
        store,
        FakeProvider("primary", result=fetched("primary", 106, 106)),
    ).execute()

    assert result.snapshot.status is UniverseSnapshotStatus.LKG
    assert result.snapshot.members == previous.members
    assert "grew too far" in result.provider_errors[0]


@pytest.mark.asyncio
async def test_all_fail_without_lkg_raises_explicit_error() -> None:
    store = Store()
    with pytest.raises(AllUniverseProvidersFailed, match="primary"):
        await service(store, FakeProvider("primary", error=RuntimeError("down"))).execute()


@pytest.mark.asyncio
async def test_primary_success_publishes_fresh_snapshot() -> None:
    store = Store(snapshot())
    result = await service(store, FakeProvider("primary", result=fetched("primary", 100))).execute()

    assert result.snapshot.status is UniverseSnapshotStatus.PRIMARY
    assert result.snapshot.coverage == 1
    assert result.provider_errors == ()
    assert store.published == [result.snapshot]


@pytest.mark.asyncio
async def test_persistence_failure_does_not_masquerade_as_provider_fallback() -> None:
    store = Store(snapshot(), publish_error=RuntimeError("database unavailable"))
    secondary = FakeProvider("secondary", result=fetched("secondary", 100))

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service(
            store,
            FakeProvider("primary", result=fetched("primary", 100)),
            secondary,
        ).execute()

    assert store.published == []
    assert secondary.calls == 0
