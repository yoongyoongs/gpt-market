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
from app.v3.domain.evidence import (
    EntityLink,
    EvidenceConflict,
    EvidenceFetchRun,
    EvidenceRelation,
    EvidenceReadQuery,
    EvidenceRepositoryPage,
    EvidenceSource,
    NormalizedEvidence,
    ParseAttempt,
    RawDocument,
    SecurityEvidenceView,
)
from app.v3.domain.recall import (
    PerformanceObservation,
    RawOpportunity,
    RecallChannel,
    RecallFeatureView,
    RecallResult,
    RecallReadPage,
    RecallRun,
    RawOpportunityReadPage,
)


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

    async def get_run(self, feature_run_id: UUID) -> FeatureRun | None: ...

    async def latest_run(self) -> FeatureRun | None: ...

    async def features_for_run(self, feature_run_id: UUID) -> tuple[RecallFeatureView, ...]: ...


class RecallRepository(Protocol):
    async def resolve_channels(
        self, channels: tuple[RecallChannel, ...]
    ) -> dict[str, UUID]: ...

    async def publish(
        self,
        run: RecallRun,
        results: tuple[RecallResult, ...],
        raw_opportunities: tuple[RawOpportunity, ...],
        observations: tuple[PerformanceObservation, ...],
    ) -> bool: ...

    async def get_run_by_content_hash(self, content_hash: str) -> RecallRun | None: ...

    async def read_results(
        self,
        *,
        recall_run_id: UUID | None,
        channel_code: str | None,
        limit: int,
        cursor: str | None,
    ) -> RecallReadPage | None: ...

    async def read_raw(
        self,
        *,
        recall_run_id: UUID | None,
        limit: int,
        cursor: str | None,
    ) -> RawOpportunityReadPage | None: ...


class EvidenceRepository(Protocol):
    async def upsert_source(self, source: EvidenceSource) -> UUID: ...

    async def add_fetch_run(self, run: EvidenceFetchRun) -> None: ...

    async def get_fetch_run(self, fetch_run_id: UUID) -> EvidenceFetchRun | None: ...

    async def save_fetch_run(self, run: EvidenceFetchRun, *, expected_version: int) -> bool: ...

    async def add_raw_if_absent(self, document: RawDocument) -> bool: ...

    async def get_raw(self, raw_document_id: UUID) -> RawDocument | None: ...

    async def find_raw(
        self, *, evidence_source_id: UUID, document_key: str, content_hash: str
    ) -> RawDocument | None: ...

    async def publish_parse(
        self,
        attempt: ParseAttempt,
        records: tuple[NormalizedEvidence, ...],
        links: tuple[EntityLink, ...],
        relations: tuple[EvidenceRelation, ...] = (),
        conflicts: tuple[EvidenceConflict, ...] = (),
    ) -> bool: ...

    async def records_for_claim(
        self, *, subject_type: str, subject_id: str, claim_key: str, as_of: datetime
    ) -> tuple[NormalizedEvidence, ...]: ...

    async def retrieve(
        self, *, subject_type: str, subject_id: str, as_of: datetime, limit: int
    ) -> tuple[NormalizedEvidence, ...]: ...

    async def retrieve_view(
        self, *, query: EvidenceReadQuery
    ) -> EvidenceRepositoryPage: ...

    async def for_securities(
        self, security_ids: tuple[UUID, ...], *, as_of: datetime
    ) -> tuple[SecurityEvidenceView, ...]: ...


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
    evidence: EvidenceRepository
    recalls: RecallRepository

    async def __aenter__(self) -> "UnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
