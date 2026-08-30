from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import (
    EvidenceReadItem,
    EvidenceReadPage,
    EvidenceReadQuery,
    EvidenceTypeCoverage,
)
from app.v3.repositories.protocols import UnitOfWork


class ReadEvidenceService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def execute(self, query: EvidenceReadQuery) -> EvidenceReadPage:
        async with self._uow_factory() as uow:
            repository_page = await uow.evidence.retrieve_view(query=query)
        items = [
            EvidenceReadItem(
                record=view.record,
                match_type=view.match_type,
                effective_relevance=view.record.effective_relevance(query.as_of),
                conflict_status=view.conflict_status,
            )
            for view in repository_page.views
        ]
        items.sort(key=lambda item: (
            -item.effective_relevance,
            item.record.source_priority,
            -item.record.known_at.timestamp(),
            str(item.record.evidence_id),
        ))
        requested_types = query.evidence_types or tuple(EvidenceType)
        counts = Counter(repository_page.coverage_counts)
        coverage = tuple(
            EvidenceTypeCoverage(
                evidence_type=evidence_type,
                status="AVAILABLE" if counts[evidence_type] else "UNKNOWN",
                count=counts[evidence_type],
            )
            for evidence_type in requested_types
        )
        return EvidenceReadPage(
            subject_type=query.subject_type,
            subject_id=query.subject_id,
            as_of=query.as_of,
            items=tuple(items),
            coverage=coverage,
        )
