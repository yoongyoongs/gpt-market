from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID

from app.v3.application.ingest_evidence import IngestEvidenceBatchService
from app.v3.domain.evidence import EvidenceFetchRun, FetchRunStatus
from app.v3.providers.evidence import EvidenceParser, EvidenceProvider
from app.v3.repositories.protocols import UnitOfWork


class RunEvidenceIngestionService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        batch_service: IngestEvidenceBatchService | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._batch_service = batch_service or IngestEvidenceBatchService(
            uow_factory, clock=clock
        )

    async def execute(
        self,
        *,
        provider: EvidenceProvider,
        parser: EvidenceParser,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        fetch_run_id: UUID | None = None,
        max_batches: int | None = None,
    ) -> EvidenceFetchRun:
        if max_batches is not None and max_batches < 1:
            raise ValueError("max_batches must be positive")
        source_id = await self._upsert_source(provider)
        if fetch_run_id is None:
            run = EvidenceFetchRun(
                evidence_source_id=source_id,
                window_start=window_start,
                window_end=window_end,
                started_at=self._clock(),
            )
            async with self._uow_factory() as uow:
                await uow.evidence.add_fetch_run(run)
                await uow.commit()
        else:
            async with self._uow_factory() as uow:
                run = await uow.evidence.get_fetch_run(fetch_run_id)
            if run is None:
                raise ValueError("evidence fetch run does not exist")
            if run.evidence_source_id != source_id:
                raise ValueError("fetch run belongs to a different evidence source")
            if run.status is not FetchRunStatus.RUNNING:
                return run
            if run.window_start != window_start or run.window_end != window_end:
                raise ValueError("resume window does not match the existing fetch run")

        batches = 0
        while max_batches is None or batches < max_batches:
            previous_version = run.row_version
            try:
                result = await self._batch_service.execute(
                    provider=provider,
                    parser=parser,
                    window_start=run.window_start,
                    window_end=run.window_end,
                    cursor=run.cursor or None,
                )
            except Exception as exc:
                run = run.fail(
                    completed_at=self._clock(),
                    error=f"{type(exc).__name__}: {exc}",
                )
                await self._save(run, expected_version=previous_version)
                return run
            run = run.checkpoint(
                cursor=result.next_cursor,
                expected_count=result.upstream_count,
                fetched_count=result.fetched_count,
                raw_inserted_count=result.raw_inserted_count,
                duplicate_count=result.duplicate_count,
                parsed_count=result.parsed_document_count,
                evidence_count=result.evidence_count,
                failed_count=result.failed_count,
                errors=result.errors,
            )
            run = run.finish(completed_at=self._clock(), exhausted=result.exhausted)
            await self._save(run, expected_version=previous_version)
            batches += 1
            if run.status is not FetchRunStatus.RUNNING:
                return run
        return run

    async def _upsert_source(self, provider: EvidenceProvider) -> UUID:
        async with self._uow_factory() as uow:
            source_id = await uow.evidence.upsert_source(provider.source)
            await uow.commit()
        return source_id

    async def _save(self, run: EvidenceFetchRun, *, expected_version: int) -> None:
        async with self._uow_factory() as uow:
            saved = await uow.evidence.save_fetch_run(
                run, expected_version=expected_version
            )
            if not saved:
                raise RuntimeError("evidence fetch run checkpoint conflict")
            await uow.commit()
