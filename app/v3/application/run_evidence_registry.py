from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import Field

from app.v3.contracts.base import V3Contract
from app.v3.domain.evidence import EvidenceFetchRun, FetchRunStatus
from app.v3.providers.evidence import EvidenceCapability, EvidenceRegistry


class EvidenceRunService(Protocol):
    async def execute(self, **kwargs) -> EvidenceFetchRun: ...


class CapabilityRunStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceSourceAttempt(V3Contract):
    capability: EvidenceCapability
    source_code: str
    fetch_run_id: UUID | None = None
    status: FetchRunStatus
    fetched_count: int = 0
    raw_inserted_count: int = 0
    duplicate_count: int = 0
    parsed_count: int = 0
    evidence_count: int = 0
    failed_count: int = 0
    errors: dict[str, str] = Field(default_factory=dict)


class EvidenceCapabilityRun(V3Contract):
    capability: EvidenceCapability
    status: CapabilityRunStatus
    selected_source: str | None = None
    attempts: tuple[EvidenceSourceAttempt, ...] = ()


class EvidenceRegistryRun(V3Contract):
    capabilities: tuple[EvidenceCapabilityRun, ...]


class RunEvidenceRegistryService:
    def __init__(
        self,
        registry: EvidenceRegistry,
        run_service: EvidenceRunService,
    ) -> None:
        self._registry = registry
        self._run_service = run_service

    async def execute(
        self,
        *,
        capabilities: tuple[EvidenceCapability, ...],
        window_start: datetime | None,
        window_end: datetime | None,
        fetch_run_ids: dict[str, UUID] | None = None,
        max_batches: int | None = None,
        collect_all: bool = False,
    ) -> EvidenceRegistryRun:
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("evidence capabilities must be unique")
        resume_ids = fetch_run_ids or {}
        results = []
        for capability in capabilities:
            bindings = self._registry.providers_for(capability)
            if not bindings:
                results.append(
                    EvidenceCapabilityRun(
                        capability=capability,
                        status=CapabilityRunStatus.UNAVAILABLE,
                    )
                )
                continue
            attempts = []
            selected_source = None
            saw_partial = False
            for provider, parser in bindings:
                try:
                    run = await self._run_service.execute(
                        provider=provider,
                        parser=parser,
                        window_start=window_start,
                        window_end=window_end,
                        fetch_run_id=resume_ids.get(provider.source.code),
                        max_batches=max_batches,
                    )
                    attempt = EvidenceSourceAttempt(
                        capability=capability,
                        source_code=provider.source.code,
                        fetch_run_id=run.fetch_run_id,
                        status=run.status,
                        fetched_count=run.fetched_count,
                        raw_inserted_count=run.raw_inserted_count,
                        duplicate_count=run.duplicate_count,
                        parsed_count=run.parsed_count,
                        evidence_count=run.evidence_count,
                        failed_count=run.failed_count,
                        errors=run.errors,
                    )
                except Exception as exc:
                    attempt = EvidenceSourceAttempt(
                        capability=capability,
                        source_code=provider.source.code,
                        status=FetchRunStatus.FAILED,
                        failed_count=1,
                        errors={"runner": f"{type(exc).__name__}: {exc}"},
                    )
                attempts.append(attempt)
                if attempt.status in {FetchRunStatus.RUNNING, FetchRunStatus.COMPLETED}:
                    selected_source = selected_source or attempt.source_code
                    if not collect_all:
                        break
                saw_partial = saw_partial or attempt.status is FetchRunStatus.PARTIAL
            if selected_source is not None:
                status = CapabilityRunStatus.SUCCESS
            elif saw_partial:
                status = CapabilityRunStatus.PARTIAL
            else:
                status = CapabilityRunStatus.FAILED
            results.append(
                EvidenceCapabilityRun(
                    capability=capability,
                    status=status,
                    selected_source=selected_source,
                    attempts=tuple(attempts),
                )
            )
        return EvidenceRegistryRun(capabilities=tuple(results))
