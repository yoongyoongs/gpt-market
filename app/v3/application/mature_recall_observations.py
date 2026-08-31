from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import Field

from app.v3.contracts.base import V3Contract
from app.v3.domain.recall import (
    ObservationStatus,
    PerformanceObservation,
    RecallMissEvaluation,
)
from app.v3.providers.recall import ObservationOutcomeProvider
from app.v3.repositories.protocols import UnitOfWork


class RecallMissThreshold(V3Contract):
    version: str = Field(min_length=1, max_length=64)
    raw_return_gte: float
    excess_return_gte: float | None = None

    def is_exceptional(
        self, *, raw_return: float, excess_return: float | None
    ) -> bool:
        return raw_return >= self.raw_return_gte or (
            self.excess_return_gte is not None
            and excess_return is not None
            and excess_return >= self.excess_return_gte
        )

    def specification(self) -> dict[str, object]:
        return {
            "logic": "ANY",
            "raw_return_gte": self.raw_return_gte,
            "excess_return_gte": self.excess_return_gte,
        }


class MatureRecallResult(V3Contract):
    requested_count: int = Field(ge=0)
    matured_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    evaluation_count: int = Field(ge=0)
    miss_count: int = Field(ge=0)
    inserted_count: int = Field(ge=0)


class MatureRecallObservationsService:
    """Append mature outcomes without mutating the original PENDING facts.

    Outcome acquisition intentionally happens outside a database transaction. The
    provider is responsible for a point-in-time-safe price/benchmark calculation;
    Phase 5 only owns lifecycle validation, append-only persistence and basic miss
    classification. MFE/MAE and attribution remain Phase 10 responsibilities.
    """

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        outcome_provider: ObservationOutcomeProvider,
        *,
        threshold: RecallMissThreshold,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow_factory = uow_factory
        self._outcome_provider = outcome_provider
        self._threshold = threshold
        self._clock = clock

    async def execute(self, *, limit: int = 1000) -> MatureRecallResult:
        if limit < 1:
            raise ValueError("limit must be positive")
        evaluated_at = self._clock()
        async with self._uow_factory() as uow:
            pending = await uow.recalls.pending_observations(
                as_of=evaluated_at, limit=limit
            )
        if not pending:
            return MatureRecallResult(
                requested_count=0,
                matured_count=0,
                unavailable_count=0,
                evaluation_count=0,
                miss_count=0,
                inserted_count=0,
            )

        outcomes = await self._outcome_provider.resolve(pending, as_of=evaluated_at)
        by_id = {item.pending_observation_id: item for item in outcomes}
        if len(by_id) != len(outcomes):
            raise ValueError("outcome provider returned duplicate observation ids")
        expected_ids = {item.observation_id for item in pending}
        if set(by_id) != expected_ids:
            raise ValueError("outcome provider must return exactly one result per observation")

        async with self._uow_factory() as uow:
            recalled_keys = await uow.recalls.recalled_security_keys(pending)

        terminal = []
        evaluations = []
        for source in pending:
            outcome = by_id[source.observation_id]
            common = {
                "recall_run_id": source.recall_run_id,
                "security_id": source.security_id,
                "horizon_sessions": source.horizon_sessions,
                "as_of": source.as_of,
                "matures_at": source.matures_at,
                "known_at": evaluated_at,
                "baseline_price": source.baseline_price,
                "supersedes_observation_id": source.observation_id,
            }
            if not outcome.available:
                terminal.append(PerformanceObservation.build(
                    **common,
                    status=ObservationStatus.UNAVAILABLE,
                    unavailable_reason=outcome.unavailable_reason,
                ))
                continue

            assert outcome.future_price is not None
            raw_return = round(outcome.future_price / source.baseline_price - 1, 10)
            excess_return = (
                None
                if outcome.benchmark_return is None
                else round(raw_return - outcome.benchmark_return, 10)
            )
            matured = PerformanceObservation.build(
                **common,
                status=ObservationStatus.MATURED,
                future_price=outcome.future_price,
                raw_return=raw_return,
                benchmark_return=outcome.benchmark_return,
                excess_return=excess_return,
            )
            terminal.append(matured)
            was_recalled = (source.recall_run_id, source.security_id) in recalled_keys
            exceptional = self._threshold.is_exceptional(
                raw_return=raw_return, excess_return=excess_return
            )
            miss_type = (
                "UNRECALLED_EXCEPTIONAL_RETURN"
                if exceptional and not was_recalled
                else None
            )
            evaluations.append(RecallMissEvaluation.build(
                observation_id=matured.observation_id,
                threshold_version=self._threshold.version,
                threshold_spec=self._threshold.specification(),
                was_recalled=was_recalled,
                is_exceptional=exceptional,
                miss_type=miss_type,
                evaluated_at=evaluated_at,
                known_at=evaluated_at,
            ))

        async with self._uow_factory() as uow:
            inserted_ids = await uow.recalls.publish_maturities(
                tuple(terminal), tuple(evaluations)
            )
            await uow.commit()
        matured_count = sum(
            item.observation_id in inserted_ids
            and item.status is ObservationStatus.MATURED
            for item in terminal
        )
        unavailable_count = sum(
            item.observation_id in inserted_ids
            and item.status is ObservationStatus.UNAVAILABLE
            for item in terminal
        )
        persisted_evaluations = tuple(
            item for item in evaluations if item.observation_id in inserted_ids
        )
        return MatureRecallResult(
            requested_count=len(pending),
            matured_count=matured_count,
            unavailable_count=unavailable_count,
            evaluation_count=len(persisted_evaluations),
            miss_count=sum(item.miss_type is not None for item in persisted_evaluations),
            inserted_count=len(inserted_ids),
        )
