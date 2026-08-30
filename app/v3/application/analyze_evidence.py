from __future__ import annotations

import re
from collections.abc import Callable

from app.v3.contracts.base import V3Contract
from app.v3.domain.evidence import (
    ConflictStatus,
    EvidenceConflict,
    EvidenceRelation,
    EvidenceRelationType,
    NormalizedEvidence,
)
from app.v3.domain.hashing import canonical_hash, canonical_json
from app.v3.repositories.protocols import UnitOfWork


TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


class EvidenceAnalysis(V3Contract):
    relations: tuple[EvidenceRelation, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()


class AnalyzeEvidenceService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        near_duplicate_threshold: float = 0.92,
    ) -> None:
        if not 0 < near_duplicate_threshold <= 1:
            raise ValueError("near_duplicate_threshold must be in (0, 1]")
        self._uow_factory = uow_factory
        self._near_duplicate_threshold = near_duplicate_threshold

    async def execute(
        self, records: tuple[NormalizedEvidence, ...]
    ) -> EvidenceAnalysis:
        relations: list[EvidenceRelation] = []
        conflicts: list[EvidenceConflict] = []
        pending: dict[tuple[str, str, str], list[NormalizedEvidence]] = {}
        for record in records:
            key = (record.subject_type, record.subject_id, record.claim_key)
            async with self._uow_factory() as uow:
                persisted = await uow.evidence.records_for_claim(
                    subject_type=record.subject_type,
                    subject_id=record.subject_id,
                    claim_key=record.claim_key,
                    as_of=record.known_at,
                )
            candidates = [*persisted, *pending.get(key, [])]
            conflicting = []
            for candidate in candidates:
                relation = self._relation(record, candidate)
                if relation is not None:
                    relations.append(relation)
                else:
                    conflicting.append(candidate)
            if conflicting:
                members = tuple([*candidates, record])
                selected = min(
                    members,
                    key=lambda item: (
                        item.source_priority,
                        -item.confidence,
                        -item.known_at.timestamp(),
                        str(item.evidence_id),
                    ),
                )
                conflicts.append(EvidenceConflict.build(
                    subject_type=record.subject_type,
                    subject_id=record.subject_id,
                    claim_key=record.claim_key,
                    status=ConflictStatus.OPEN,
                    selected_evidence_id=selected.evidence_id,
                    resolution=(
                        "provisional preference by source_priority, confidence and known_at; "
                        "all conflicting evidence remains available"
                    ),
                    member_ids=tuple(item.evidence_id for item in members),
                ))
            pending.setdefault(key, []).append(record)
        return EvidenceAnalysis(
            relations=tuple(relations),
            conflicts=tuple(conflicts),
        )

    def _relation(
        self, current: NormalizedEvidence, candidate: NormalizedEvidence
    ) -> EvidenceRelation | None:
        current_hash = canonical_hash(current.normalized_payload)
        candidate_hash = canonical_hash(candidate.normalized_payload)
        if current_hash == candidate_hash:
            return EvidenceRelation.build(
                from_evidence_id=current.evidence_id,
                to_evidence_id=candidate.evidence_id,
                relation_type=EvidenceRelationType.EXACT_DUPLICATE,
                similarity=1.0,
                reason="identical normalized payload",
            )
        similarity = self._jaccard(
            canonical_json(current.normalized_payload),
            canonical_json(candidate.normalized_payload),
        )
        if similarity >= self._near_duplicate_threshold:
            return EvidenceRelation.build(
                from_evidence_id=current.evidence_id,
                to_evidence_id=candidate.evidence_id,
                relation_type=EvidenceRelationType.NEAR_DUPLICATE,
                similarity=similarity,
                reason="normalized payload token similarity above threshold",
            )
        return None

    @staticmethod
    def _jaccard(left: str, right: str) -> float:
        left_tokens = set(TOKEN_RE.findall(left.lower()))
        right_tokens = set(TOKEN_RE.findall(right.lower()))
        union = left_tokens | right_tokens
        return 1.0 if not union else len(left_tokens & right_tokens) / len(union)
