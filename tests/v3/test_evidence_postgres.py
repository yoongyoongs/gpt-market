from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.contracts.evidence import EvidenceType
from app.v3.application.ingest_evidence import IngestEvidenceBatchService, normalize_reference
from app.v3.domain.evidence import (
    DecayModel,
    EntityLink,
    EntityLinkStatus,
    EvidenceSource,
    EvidenceSourceType,
    FetchedDocument,
    NormalizedEvidence,
    ParseAttempt,
    ParseStatus,
    RawDocument,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.providers.evidence import EvidenceFetchBatch, ParsedEvidenceBundle


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured")
NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)


class FixtureProvider:
    def __init__(self, source: EvidenceSource, documents: tuple[FetchedDocument, ...]) -> None:
        self.source = source
        self.documents = documents

    async def fetch(self, **_kwargs) -> EvidenceFetchBatch:
        return EvidenceFetchBatch(documents=self.documents, exhausted=True)

    async def close(self) -> None:
        return None


class FixtureParser:
    code = "fixture"
    version = "fixture-v1"

    def parse(self, raw: RawDocument, source: EvidenceSource) -> ParsedEvidenceBundle:
        if raw.payload_text == "invalid":
            raise ValueError("fixture parse failure")
        record = make_evidence(raw)
        link = EntityLink.build(
            evidence_id=record.evidence_id,
            entity_type="SECURITY",
            entity_id=record.subject_id,
            match_basis={"field": "code", "value": "600519"},
            confidence=1,
            status=EntityLinkStatus.CONFIRMED,
        )
        return ParsedEvidenceBundle(records=(record,), links=(link,))


def make_evidence(
    raw: RawDocument, *, known_at: datetime = NOW, subject_id: str = "SH:600519"
) -> NormalizedEvidence:
    return NormalizedEvidence.build(
        raw_document_id=raw.raw_document_id,
        evidence_type=EvidenceType.OFFICIAL_DISCLOSURE,
        source_type=EvidenceSourceType.OFFICIAL,
        subject_type="SECURITY",
        subject_id=subject_id,
        claim_key="disclosure:fixture-1",
        source="fixture-official",
        upstream_source="fixture-exchange",
        payload={"title": "fixture disclosure"},
        normalized_payload={"title": "fixture disclosure", "category": "ANNOUNCEMENT"},
        event_time=known_at - timedelta(hours=2),
        publish_time=known_at - timedelta(hours=1),
        fetch_time=known_at,
        known_at=known_at,
        confidence=1,
        relevance=0.9,
        decay_model=DecayModel.NONE,
        parser_version="fixture-v1",
    )


@pytest.mark.asyncio
async def test_evidence_raw_parse_retrieve_dedup_and_immutability() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    subject_id = f"SH:{uuid4().hex[:12]}"
    source = EvidenceSource(
        code=f"fixture-official-{uuid4().hex}",
        source_type=EvidenceSourceType.OFFICIAL,
        upstream_source="fixture-exchange",
        capabilities={"types": ["OFFICIAL_DISCLOSURE"]},
        priority=1,
        parser_version="fixture-v1",
        reliability=1,
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        source_id = await uow.evidence.upsert_source(source)
        await uow.commit()

    fetched = FetchedDocument(
        document_key="fixture-1",
        raw_reference="https://example.invalid/disclosure/fixture-1?b=2&a=1",
        mime_type="application/json",
        payload_text='{"title":"fixture disclosure","code":"600519"}',
        fetch_time=NOW,
        known_at=NOW,
    )
    raw = RawDocument.build(
        evidence_source_id=source_id,
        fetched=fetched,
        normalized_reference="https://example.invalid/disclosure/fixture-1?a=1&b=2",
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.evidence.add_raw_if_absent(raw) is True
        assert await uow.evidence.add_raw_if_absent(raw) is False
        await uow.commit()

    record = make_evidence(raw, subject_id=subject_id)
    link = EntityLink.build(
        evidence_id=record.evidence_id,
        entity_type="SECURITY",
        entity_id=subject_id,
        match_basis={"field": "code", "value": "600519"},
        confidence=1,
        status=EntityLinkStatus.CONFIRMED,
    )
    attempt = ParseAttempt.build(
        raw_document_id=raw.raw_document_id,
        parser_code="fixture",
        parser_version="fixture-v1",
        status=ParseStatus.SUCCESS,
        output_count=1,
        error=None,
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.evidence.publish_parse(attempt, (record,), (link,)) is True
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.evidence.publish_parse(attempt, (record,), (link,)) is False
        historical = await uow.evidence.retrieve(
            subject_type="SECURITY", subject_id=subject_id,
            as_of=NOW - timedelta(seconds=1), limit=20,
        )
        current = await uow.evidence.retrieve(
            subject_type="SECURITY", subject_id=subject_id,
            as_of=NOW + timedelta(seconds=2), limit=20,
        )
    assert historical == ()
    assert [item.evidence_id for item in current] == [record.evidence_id]

    second_source = source.model_copy(update={
        "evidence_source_id": uuid4(),
        "code": f"fixture-official-copy-{uuid4().hex}",
    })
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        second_source_id = await uow.evidence.upsert_source(second_source)
        same_content = RawDocument.build(
            evidence_source_id=second_source_id,
            fetched=fetched.model_copy(update={"document_key": "fixture-copy"}),
            normalized_reference="https://example.invalid/disclosure/fixture-copy",
        )
        assert same_content.content_hash == raw.content_hash
        assert await uow.evidence.add_raw_if_absent(same_content) is True
        await uow.commit()

    async with engine.connect() as connection:
        with pytest.raises(DBAPIError, match="immutable V3 record"):
            await connection.execute(
                text("UPDATE v3.raw_documents SET parser_status='SUCCESS' WHERE raw_document_id=:id"),
                {"id": raw.raw_document_id},
            )
        await connection.rollback()
        with pytest.raises(DBAPIError, match="immutable V3 record"):
            await connection.execute(
                text("DELETE FROM v3.raw_document_parse_attempts WHERE parse_attempt_id=:id"),
                {"id": attempt.parse_attempt_id},
            )
        await connection.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingestion_service_commits_raw_before_parse_and_replays_idempotently() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex
    source = EvidenceSource(
        code=f"fixture-pipeline-{suffix}",
        source_type=EvidenceSourceType.OFFICIAL,
        upstream_source="fixture-exchange",
        capabilities={"types": ["OFFICIAL_DISCLOSURE"]},
        priority=1,
        parser_version="fixture-v1",
        reliability=1,
    )
    documents = (
        FetchedDocument(
            document_key=f"valid-{suffix}",
            raw_reference=f"HTTPS://EXAMPLE.INVALID:443/path?b=2&a=1#{suffix}",
            mime_type="application/json",
            payload_text='{"title":"fixture disclosure","code":"600519"}',
            fetch_time=NOW,
            known_at=NOW,
        ),
        FetchedDocument(
            document_key=f"invalid-{suffix}",
            raw_reference=f"https://example.invalid/path/{suffix}/invalid",
            mime_type="text/plain",
            payload_text="invalid",
            fetch_time=NOW,
            known_at=NOW,
        ),
    )
    service = IngestEvidenceBatchService(lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW)
    provider = FixtureProvider(source, documents)
    first = await service.execute(provider=provider, parser=FixtureParser())
    replay = await service.execute(provider=provider, parser=FixtureParser())
    assert first.fetched_count == 2
    assert first.raw_inserted_count == 2
    assert first.parsed_count == 1
    assert first.failed_count == 1
    assert replay.raw_inserted_count == 0
    assert replay.duplicate_count == 2
    assert replay.parsed_count == 0
    assert normalize_reference(documents[0].raw_reference) == "https://example.invalid/path?a=1&b=2"

    async with engine.connect() as connection:
        raw_count = await connection.scalar(text(
            "SELECT count(*) FROM v3.raw_documents r JOIN v3.evidence_sources s "
            "ON s.evidence_source_id=r.evidence_source_id WHERE s.code=:code"
        ), {"code": source.code})
        attempts = await connection.execute(text(
            "SELECT status, count(*) FROM v3.raw_document_parse_attempts p "
            "JOIN v3.raw_documents r ON r.raw_document_id=p.raw_document_id "
            "JOIN v3.evidence_sources s ON s.evidence_source_id=r.evidence_source_id "
            "WHERE s.code=:code GROUP BY status ORDER BY status"
        ), {"code": source.code})
    assert raw_count == 2
    assert dict(attempts.all()) == {"FAILED": 1, "SUCCESS": 1}
    await engine.dispose()
