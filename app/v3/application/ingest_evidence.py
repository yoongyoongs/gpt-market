from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field

from app.v3.contracts.base import V3Contract
from app.v3.application.analyze_evidence import AnalyzeEvidenceService
from app.v3.domain.evidence import (
    EntityLink,
    NormalizedEvidence,
    ParseAttempt,
    ParseStatus,
    RawDocument,
)
from app.v3.providers.evidence import EvidenceParser, EvidenceProvider
from app.v3.repositories.protocols import UnitOfWork


class EvidenceEntityLinker(Protocol):
    def links_for(self, records: tuple[NormalizedEvidence, ...]) -> tuple[EntityLink, ...]: ...


def normalize_reference(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("raw_reference must be an absolute HTTP(S) URL")
    host = parsed.hostname.lower()
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.lower(), host, path, query, ""))


class EvidenceIngestionResult(V3Contract):
    source_code: str
    fetched_count: int = Field(ge=0)
    raw_inserted_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    parsed_document_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    errors: dict[str, str]
    next_cursor: dict[str, object] | None = None
    exhausted: bool
    upstream_count: int | None = Field(default=None, ge=0)


class IngestEvidenceBatchService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        analyzer: AnalyzeEvidenceService | None = None,
        entity_linker: EvidenceEntityLinker | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._analyzer = analyzer or AnalyzeEvidenceService(uow_factory)
        self._entity_linker = entity_linker

    async def execute(
        self,
        *,
        provider: EvidenceProvider,
        parser: EvidenceParser,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        cursor: dict[str, object] | None = None,
    ) -> EvidenceIngestionResult:
        source = provider.source
        if parser.version != source.parser_version:
            raise ValueError("parser version does not match evidence source configuration")
        async with self._uow_factory() as uow:
            source_id = await uow.evidence.upsert_source(source)
            await uow.commit()
        batch = await provider.fetch(
            window_start=window_start,
            window_end=window_end,
            cursor=cursor,
        )
        inserted_count = 0
        duplicate_count = 0
        parsed_document_count = 0
        evidence_count = 0
        errors: dict[str, str] = {}
        for fetched in batch.documents:
            raw = RawDocument.build(
                evidence_source_id=source_id,
                fetched=fetched,
                normalized_reference=normalize_reference(fetched.raw_reference),
            )
            async with self._uow_factory() as uow:
                inserted = await uow.evidence.add_raw_if_absent(raw)
                if inserted:
                    inserted_count += 1
                    await uow.commit()
                else:
                    duplicate_count += 1
                    existing = await uow.evidence.find_raw(
                        evidence_source_id=source_id,
                        document_key=raw.document_key,
                        content_hash=raw.content_hash,
                    )
                    if existing is None:
                        raise RuntimeError("raw document conflict did not resolve to an existing row")
                    raw = existing
            started_at = self._clock()
            try:
                parsed = parser.parse(raw, source)
                links = self._merge_links(
                    parsed.links,
                    () if self._entity_linker is None else self._entity_linker.links_for(parsed.records),
                )
                self._validate_parser_output(raw, source, parsed.records, links)
                analysis = await self._analyzer.execute(parsed.records)
                completed_at = self._clock()
                attempt = ParseAttempt.build(
                    raw_document_id=raw.raw_document_id,
                    parser_code=parser.code,
                    parser_version=parser.version,
                    status=ParseStatus.SUCCESS,
                    output_count=len(parsed.records),
                    error=None,
                    started_at=started_at,
                    completed_at=completed_at,
                )
                async with self._uow_factory() as uow:
                    published = await uow.evidence.publish_parse(
                        attempt,
                        parsed.records,
                        links,
                        analysis.relations,
                        analysis.conflicts,
                    )
                    if published:
                        parsed_document_count += 1
                        evidence_count += len(parsed.records)
                        await uow.commit()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                errors[raw.document_key] = error
                attempt = ParseAttempt.build(
                    raw_document_id=raw.raw_document_id,
                    parser_code=parser.code,
                    parser_version=parser.version,
                    status=ParseStatus.FAILED,
                    output_count=0,
                    error=error,
                    started_at=started_at,
                    completed_at=self._clock(),
                )
                async with self._uow_factory() as uow:
                    published = await uow.evidence.publish_parse(attempt, (), ())
                    if published:
                        await uow.commit()
        return EvidenceIngestionResult(
            source_code=source.code,
            fetched_count=len(batch.documents),
            raw_inserted_count=inserted_count,
            duplicate_count=duplicate_count,
            parsed_document_count=parsed_document_count,
            evidence_count=evidence_count,
            failed_count=len(errors),
            errors=errors,
            next_cursor=batch.next_cursor,
            exhausted=batch.exhausted,
            upstream_count=batch.upstream_count,
        )

    @staticmethod
    def _validate_parser_output(raw, source, records, links) -> None:
        record_ids = set()
        for record in records:
            if record.raw_document_id != raw.raw_document_id:
                raise ValueError("parser output references a different raw document")
            if record.source != source.code or record.source_type is not source.source_type:
                raise ValueError("parser output source identity does not match the provider")
            if record.source_priority != source.priority:
                raise ValueError("parser output source priority does not match the provider")
            record_ids.add(record.evidence_id)
        if len(record_ids) != len(records):
            raise ValueError("parser output contains duplicate evidence IDs")
        if any(link.evidence_id not in record_ids for link in links):
            raise ValueError("entity link references evidence outside the parse bundle")

    @staticmethod
    def _merge_links(
        parser_links: tuple[EntityLink, ...], matched_links: tuple[EntityLink, ...]
    ) -> tuple[EntityLink, ...]:
        merged = {}
        for link in (*matched_links, *parser_links):
            merged[(link.evidence_id, link.entity_type, link.entity_id)] = link
        return tuple(merged.values())
