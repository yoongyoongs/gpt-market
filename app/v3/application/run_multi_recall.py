from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.v3.domain.hashing import canonical_hash
from app.v3.domain.recall import (
    ObservationStatus,
    PerformanceObservation,
    RawOpportunity,
    RecallResult,
    RecallRun,
    RecallRunStatus,
)
from app.v3.providers.calendar import TradingCalendar
from app.v3.providers.recall import RecallChannelEvaluator
from app.v3.repositories.protocols import UnitOfWork


class RunMultiRecallService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        calendar: TradingCalendar,
        *,
        channels: tuple[RecallChannelEvaluator, ...],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not channels:
            raise ValueError("at least one recall channel is required")
        codes = [item.channel.code for item in channels]
        if len(codes) != len(set(codes)):
            raise ValueError("recall channel codes must be unique")
        self._uow_factory = uow_factory
        self._calendar = calendar
        self._channels = channels
        self._clock = clock

    async def execute(
        self,
        *,
        feature_run_id: UUID,
        strategy_version: str = "multi-recall-v1",
    ) -> RecallRun:
        async with self._uow_factory() as uow:
            feature_run = await uow.features.get_run(feature_run_id)
            features = await uow.features.features_for_run(feature_run_id)
            evidence = await uow.evidence.for_securities(
                tuple(item.security_id for item in features),
                as_of=feature_run.as_of if feature_run is not None else self._clock(),
            )
        if feature_run is None:
            raise ValueError("published feature run does not exist")
        if len(features) != feature_run.successful_count:
            raise RuntimeError("published feature run is missing security feature rows")
        if self._clock() < feature_run.as_of:
            raise ValueError("feature run as_of is in the future and cannot be recalled")

        channels = tuple(item.channel for item in self._channels)
        async with self._uow_factory() as uow:
            channel_ids = await uow.recalls.resolve_channels(channels)
            await uow.commit()

        evaluations = []
        errors = {}
        for evaluator in self._channels:
            try:
                evaluations.append((evaluator, evaluator.evaluate(features, evidence)))
            except Exception as exc:
                errors[evaluator.channel.code] = f"{type(exc).__name__}: {exc}"

        completed_at = self._clock()
        successful = len(evaluations)
        hit_security_ids = {
            candidate.security_id
            for _, evaluation in evaluations
            for candidate in evaluation.candidates
        }
        run = RecallRun.build(
            recall_run_id=uuid4(),
            feature_run_id=feature_run_id,
            regime_snapshot_id=None,
            strategy_version=strategy_version,
            channel_set_hash=canonical_hash(sorted(item.content_hash for item in channels)),
            as_of=feature_run.as_of,
            known_at=completed_at,
            status=(RecallRunStatus.PUBLISHED if successful else RecallRunStatus.FAILED),
            expected_channel_count=len(channels),
            successful_channel_count=successful,
            failed_channel_count=len(channels) - successful,
            security_count=len(features),
            hit_security_count=len(hit_security_ids),
            coverage=successful / len(channels),
            errors=errors,
        )
        async with self._uow_factory() as uow:
            existing = await uow.recalls.get_run_by_content_hash(run.content_hash)
        if existing is not None:
            return existing

        results = []
        channel_code_by_id = {}
        for evaluator, evaluation in evaluations:
            channel_id = channel_ids[evaluator.channel.content_hash]
            channel_code_by_id[channel_id] = evaluator.channel.code
            for rank, candidate in enumerate(evaluation.candidates, start=1):
                results.append(RecallResult.build(
                    recall_run_id=run.recall_run_id,
                    channel_id=channel_id,
                    security_id=candidate.security_id,
                    channel_rank=rank,
                    strength=candidate.strength,
                    reasons=candidate.reasons,
                    matched_features=candidate.matched_features,
                    coverage=candidate.coverage,
                ))
        raw_opportunities = self._raw_opportunities(
            run, tuple(results), channel_code_by_id
        )
        observations = (
            self._observations(run, features)
            if run.status is RecallRunStatus.PUBLISHED
            else ()
        )
        async with self._uow_factory() as uow:
            inserted = await uow.recalls.publish(
                run, tuple(results), raw_opportunities, observations
            )
            if not inserted:
                existing = await uow.recalls.get_run_by_content_hash(run.content_hash)
                if existing is None:
                    raise RuntimeError("recall run conflict did not resolve")
                return existing
            await uow.commit()
        return run

    @staticmethod
    def _raw_opportunities(
        run: RecallRun,
        results: tuple[RecallResult, ...],
        channel_code_by_id: dict[UUID, str],
    ) -> tuple[RawOpportunity, ...]:
        grouped = defaultdict(list)
        for result in results:
            grouped[result.security_id].append(result)
        items = []
        for security_id in sorted(grouped, key=str):
            hits = sorted(
                grouped[security_id],
                key=lambda item: (channel_code_by_id[item.channel_id], item.channel_rank),
            )
            channel_codes = tuple(channel_code_by_id[item.channel_id] for item in hits)
            items.append(RawOpportunity.build(
                recall_run_id=run.recall_run_id,
                security_id=security_id,
                as_of=run.as_of,
                known_at=run.known_at,
                recall_result_ids=tuple(item.recall_result_id for item in hits),
                channel_codes=channel_codes,
                reason_summary={
                    channel_code_by_id[item.channel_id]: item.reasons for item in hits
                },
            ))
        return tuple(items)

    def _observations(self, run, features) -> tuple[PerformanceObservation, ...]:
        items = []
        for feature in features:
            for horizon in (3, 5, 10):
                items.append(PerformanceObservation.build(
                    recall_run_id=run.recall_run_id,
                    security_id=feature.security_id,
                    horizon_sessions=horizon,
                    status=ObservationStatus.PENDING,
                    as_of=run.as_of,
                    matures_at=self._maturity(run.as_of, horizon),
                    known_at=run.known_at,
                    baseline_price=feature.close,
                ))
        return tuple(items)

    def _maturity(self, as_of: datetime, horizon_sessions: int) -> datetime:
        current = as_of.date()
        remaining = horizon_sessions
        while remaining:
            current += timedelta(days=1)
            if self._calendar.is_trading_day(current):
                remaining -= 1
        return as_of.replace(year=current.year, month=current.month, day=current.day)
