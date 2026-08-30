from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import Field

from app.v3.contracts.base import V3Contract
from app.v3.domain.recall import RecallChannel, RecallFeatureView
from app.v3.domain.evidence import SecurityEvidenceView


class RecallChannelUnavailable(RuntimeError):
    pass


class RecallCandidate(V3Contract):
    security_id: UUID
    strength: float = Field(ge=0, le=1)
    reasons: tuple[str, ...] = Field(min_length=1)
    matched_features: dict[str, object]
    coverage: float = Field(ge=0, le=1)


class ChannelEvaluation(V3Contract):
    evaluated_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    candidates: tuple[RecallCandidate, ...]


class RecallChannelEvaluator(Protocol):
    channel: RecallChannel

    def evaluate(
        self,
        features: tuple[RecallFeatureView, ...],
        evidence: tuple[SecurityEvidenceView, ...],
    ) -> ChannelEvaluation: ...
