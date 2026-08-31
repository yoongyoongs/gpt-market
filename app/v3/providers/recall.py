from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import Field

from app.v3.contracts.base import V3Contract
from app.v3.domain.evidence import SecurityEvidenceView
from app.v3.domain.recall import (
    PerformanceObservation,
    RecallChannel,
    RecallFeatureView,
)


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


class ObservationOutcome(V3Contract):
    pending_observation_id: UUID
    future_price: float | None = Field(default=None, gt=0)
    benchmark_return: float | None = None
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=256)

    @property
    def available(self) -> bool:
        return self.future_price is not None

    def model_post_init(self, _context: object) -> None:
        if (self.future_price is None) == (self.unavailable_reason is None):
            raise ValueError(
                "outcome requires either future_price or unavailable_reason"
            )
        if self.future_price is None and self.benchmark_return is not None:
            raise ValueError("unavailable outcome cannot contain benchmark_return")


class ObservationOutcomeProvider(Protocol):
    async def resolve(
        self,
        observations: tuple[PerformanceObservation, ...],
        *,
        as_of: datetime,
    ) -> tuple[ObservationOutcome, ...]: ...
