from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.v3.domain.market_data import (
    UniverseFetchResult,
    UniverseSnapshot,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)
from app.v3.providers.universe import UniverseProvider
from app.v3.repositories.protocols import UnitOfWork


class AllUniverseProvidersFailed(RuntimeError):
    pass


class UniverseCoverageError(RuntimeError):
    pass


@dataclass(frozen=True)
class RefreshUniverseResult:
    snapshot: UniverseSnapshot
    provider_errors: tuple[str, ...]
    created: bool


class RefreshUniverseService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        providers: Sequence[UniverseProvider],
        *,
        minimum_members: int = 4500,
        minimum_coverage: float = 0.9,
        minimum_retention: float = 0.9,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one universe provider is required")
        self._uow_factory = uow_factory
        self._providers = tuple(providers)
        self._minimum_members = minimum_members
        self._minimum_coverage = minimum_coverage
        self._minimum_retention = minimum_retention
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(self) -> RefreshUniverseResult:
        latest = await self._latest()
        errors: list[str] = []
        for index, provider in enumerate(self._providers):
            try:
                fetched = await provider.fetch_snapshot()
                coverage = self._validate_coverage(fetched, latest)
                snapshot = self._build_snapshot(
                    fetched,
                    latest=latest,
                    status=(
                        UniverseSnapshotStatus.PRIMARY
                        if index == 0
                        else UniverseSnapshotStatus.SECONDARY
                    ),
                    coverage=coverage,
                    stale=False,
                )
            except Exception as exc:
                errors.append(f"{provider.code}: {type(exc).__name__}: {exc}")
                continue
            created = await self._publish(snapshot)
            return RefreshUniverseResult(snapshot, tuple(errors), created)

        if latest is None:
            raise AllUniverseProvidersFailed("; ".join(errors))
        now = self._clock()
        lkg = UniverseSnapshot.build(
            UniverseSnapshotContent(
                snapshot_id=uuid4(),
                source_code=latest.source_code,
                status=UniverseSnapshotStatus.LKG,
                as_of=latest.as_of,
                fetch_time=now,
                known_at=now,
                coverage=latest.coverage,
                stale=True,
                previous_snapshot_id=latest.snapshot_id,
                members=latest.members,
            )
        )
        created = await self._publish(lkg)
        return RefreshUniverseResult(lkg, tuple(errors), created)

    async def _latest(self) -> UniverseSnapshot | None:
        async with self._uow_factory() as uow:
            return await uow.universes.latest()

    async def _publish(self, snapshot: UniverseSnapshot) -> bool:
        async with self._uow_factory() as uow:
            created = await uow.universes.publish(snapshot)
            if created:
                await uow.commit()
            return created

    def _validate_coverage(
        self, fetched: UniverseFetchResult, latest: UniverseSnapshot | None
    ) -> float:
        member_count = len(fetched.members)
        baseline = max(fetched.expected_total, len(latest.members) if latest else 0)
        coverage = member_count / baseline
        if member_count < self._minimum_members:
            raise UniverseCoverageError(
                f"member count {member_count} is below minimum {self._minimum_members}"
            )
        if coverage < self._minimum_coverage:
            raise UniverseCoverageError(
                f"coverage {coverage:.4f} is below minimum {self._minimum_coverage:.4f}"
            )
        if latest and member_count / len(latest.members) < self._minimum_retention:
            raise UniverseCoverageError("member count dropped too far from latest snapshot")
        return min(coverage, 1.0)

    def _build_snapshot(
        self,
        fetched: UniverseFetchResult,
        *,
        latest: UniverseSnapshot | None,
        status: UniverseSnapshotStatus,
        coverage: float,
        stale: bool,
    ) -> UniverseSnapshot:
        known_at = max(self._clock(), fetched.fetch_time)
        return UniverseSnapshot.build(
            UniverseSnapshotContent(
                snapshot_id=uuid4(),
                source_code=fetched.source_code,
                status=status,
                as_of=fetched.as_of,
                fetch_time=fetched.fetch_time,
                known_at=known_at,
                coverage=coverage,
                stale=stale,
                previous_snapshot_id=latest.snapshot_id if latest else None,
                members=fetched.members,
            )
        )
