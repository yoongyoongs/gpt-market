from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.v3.application.read_evidence import ReadEvidenceService
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import (
    DecayModel,
    EvidenceMatchType,
    EvidenceReadQuery,
    EvidenceRepositoryPage,
    EvidenceRepositoryView,
    EvidenceSourceType,
    NormalizedEvidence,
)


NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def evidence(evidence_type: EvidenceType, relevance: float) -> NormalizedEvidence:
    return NormalizedEvidence.build(
        raw_document_id=uuid4(), evidence_type=evidence_type,
        source_type=EvidenceSourceType.NEWS, source_priority=80,
        subject_type="MARKET", subject_id="CN_A_SHARES",
        claim_key=f"fixture:{uuid4()}", source="fixture",
        payload={}, normalized_payload={}, fetch_time=NOW, known_at=NOW,
        event_time=NOW, confidence=0.8, relevance=relevance,
        decay_model=DecayModel.LINEAR, decay_rate=0.1,
        parser_version="v1",
    )


class Repo:
    def __init__(self, views):
        self.views = views

    async def retrieve_view(self, *, query):
        return EvidenceRepositoryPage(
            views=self.views,
            coverage_counts={item.record.evidence_type: 1 for item in self.views},
        )


class Uow:
    def __init__(self, views):
        self.evidence = Repo(views)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_read_service_applies_point_in_time_decay_and_reports_unknown_coverage() -> None:
    news = evidence(EvidenceType.NEWS, 0.9)
    views = (EvidenceRepositoryView(
        record=news,
        match_type=EvidenceMatchType.CONFIRMED_LINK,
        conflict_status="OPEN",
    ),)
    result = await ReadEvidenceService(lambda: Uow(views)).execute(EvidenceReadQuery(
        subject_type="SECURITY", subject_id="SH:600519",
        as_of=NOW + timedelta(days=2),
        evidence_types=(EvidenceType.NEWS, EvidenceType.OFFICIAL_DISCLOSURE),
    ))
    assert result.items[0].effective_relevance == pytest.approx(0.72)
    assert result.items[0].conflict_status == "OPEN"
    assert [(item.evidence_type, item.status) for item in result.coverage] == [
        (EvidenceType.NEWS, "AVAILABLE"),
        (EvidenceType.OFFICIAL_DISCLOSURE, "UNKNOWN"),
    ]
