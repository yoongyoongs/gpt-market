from __future__ import annotations

from datetime import date, datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID

from app.v3.contracts.agent import AgentTask
from app.v3.domain.audit import AuditEvent
from app.v3.domain.market_data import (
    AdjustmentFactorRevision,
    AdjustType,
    BarIngestionTarget,
    BarPeriod,
    BarSeriesRevision,
    CorporateAction,
    MarketDataIngestionRun,
    UniverseSnapshot,
)
from app.v3.domain.features import FeaturePage, FeatureQuery, FeatureRun, MarketRegimeSnapshot, SecurityFeature


class AgentTaskRepository(Protocol):
    async def add_if_absent(self, task: AgentTask) -> bool: ...


class AuditRepository(Protocol):
    async def add(self, event: AuditEvent) -> None: ...


class UniverseRepository(Protocol):
    async def latest(self) -> UniverseSnapshot | None: ...

    async def publish(self, snapshot: UniverseSnapshot) -> bool: ...

    async def targets(self, snapshot_id) -> tuple[BarIngestionTarget, ...]: ...


class BarRepository(Protocol):
    async def latest_factor_revision_id(self, security_id: UUID) -> UUID | None: ...

    async def latest_series_revision_ids(
        self, security_id: UUID
    ) -> dict[tuple[BarPeriod, AdjustType], UUID]: ...

    async def publish_factor_revision(self, revision: AdjustmentFactorRevision) -> bool: ...

    async def publish_series_revision(self, revision: BarSeriesRevision) -> bool: ...

    async def has_daily_coverage(
        self, security_id: UUID, *, minimum_bars: int, minimum_last_bar_date: date
    ) -> bool: ...

    async def covered_daily_security_ids(
        self,
        targets: tuple[BarIngestionTarget, ...],
        *,
        minimum_bars: int,
        minimum_last_bar_date: date,
    ) -> set[UUID]: ...

    async def latest_daily_revisions(
        self, security_ids: tuple[UUID, ...], *, as_of: datetime
    ) -> tuple[BarSeriesRevision, ...]: ...


class FeatureRepository(Protocol):
    async def publish(
        self,
        run: FeatureRun,
        features: tuple[SecurityFeature, ...],
        regime: MarketRegimeSnapshot,
    ) -> bool: ...

    async def query(self, query: FeatureQuery) -> FeaturePage | None: ...

    async def latest_regime(self) -> MarketRegimeSnapshot | None: ...

    async def get_run_by_content_hash(self, content_hash: str) -> FeatureRun | None: ...


class CorporateActionRepository(Protocol):
    async def latest_by_source_references(
        self, source: str, references: tuple[str, ...]
    ) -> dict[str, CorporateAction]: ...

    async def publish(self, action: CorporateAction) -> bool: ...


class IngestionRunRepository(Protocol):
    async def add(self, run: MarketDataIngestionRun) -> None: ...

    async def get(self, run_id) -> MarketDataIngestionRun | None: ...

    async def save(self, run: MarketDataIngestionRun, *, expected_version: int) -> bool: ...


class UnitOfWork(Protocol):
    tasks: AgentTaskRepository
    audits: AuditRepository
    universes: UniverseRepository
    bars: BarRepository
    corporate_actions: CorporateActionRepository
    ingestion_runs: IngestionRunRepository
    features: FeatureRepository

    async def __aenter__(self) -> "UnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
