from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.v3.application.mature_recall_observations import (
    MatureRecallObservationsService,
    RecallMissThreshold,
)
from app.v3.domain.recall import ObservationStatus, PerformanceObservation
from app.v3.providers.recall import ObservationOutcome

NOW = datetime(2026, 9, 10, 8, tzinfo=timezone.utc)


def pending(*, run_id, security_id, baseline=10) -> PerformanceObservation:
    return PerformanceObservation.build(
        recall_run_id=run_id,
        security_id=security_id,
        horizon_sessions=5,
        status=ObservationStatus.PENDING,
        as_of=NOW - timedelta(days=8),
        matures_at=NOW - timedelta(days=1),
        known_at=NOW - timedelta(days=8),
        baseline_price=baseline,
    )


class Outcomes:
    def __init__(self, values):
        self.values = values

    async def resolve(self, observations, *, as_of):
        assert as_of == NOW
        assert observations
        return self.values


class Recalls:
    def __init__(self, observations, recalled):
        self.pending = observations
        self.recalled = recalled
        self.published = None

    async def pending_observations(self, *, as_of, limit):
        if self.published is not None:
            return ()
        return self.pending[:limit]

    async def recalled_security_keys(self, _observations):
        return self.recalled

    async def publish_maturities(self, observations, evaluations):
        self.published = observations, evaluations
        return {item.observation_id for item in observations}


class Uow:
    def __init__(self, recalls):
        self.recalls = recalls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_maturity_appends_results_and_only_marks_exceptional_unrecalled_as_miss() -> None:
    run_id = uuid4()
    recalled_security = uuid4()
    missed_security = uuid4()
    unavailable_security = uuid4()
    rows = (
        pending(run_id=run_id, security_id=recalled_security),
        pending(run_id=run_id, security_id=missed_security),
        pending(run_id=run_id, security_id=unavailable_security),
    )
    outcomes = Outcomes((
        ObservationOutcome(
            pending_observation_id=rows[0].observation_id,
            future_price=12,
            benchmark_return=0.05,
        ),
        ObservationOutcome(
            pending_observation_id=rows[1].observation_id,
            future_price=13,
            benchmark_return=0.08,
        ),
        ObservationOutcome(
            pending_observation_id=rows[2].observation_id,
            unavailable_reason="point-in-time-safe outcome is unavailable",
        ),
    ))
    recalls = Recalls(rows, {(run_id, recalled_security)})
    service = MatureRecallObservationsService(
        lambda: Uow(recalls),
        outcomes,
        threshold=RecallMissThreshold(
            version="phase5-return-v1",
            raw_return_gte=0.1,
            excess_return_gte=0.1,
        ),
        clock=lambda: NOW,
    )

    result = await service.execute()
    replay = await service.execute()

    assert result.model_dump() == {
        "requested_count": 3,
        "matured_count": 2,
        "unavailable_count": 1,
        "evaluation_count": 2,
        "miss_count": 1,
        "inserted_count": 3,
    }
    assert replay.requested_count == replay.inserted_count == 0
    terminal, evaluations = recalls.published
    assert all(item.supersedes_observation_id for item in terminal)
    assert {item.status for item in terminal} == {
        ObservationStatus.MATURED,
        ObservationStatus.UNAVAILABLE,
    }
    assert [item.miss_type for item in evaluations].count(
        "UNRECALLED_EXCEPTIONAL_RETURN"
    ) == 1
    assert all(item.status is ObservationStatus.PENDING for item in rows)


@pytest.mark.asyncio
async def test_maturity_rejects_incomplete_provider_result_without_persisting() -> None:
    row = pending(run_id=uuid4(), security_id=uuid4())
    recalls = Recalls((row,), set())
    service = MatureRecallObservationsService(
        lambda: Uow(recalls),
        Outcomes(()),
        threshold=RecallMissThreshold(
            version="phase5-return-v1", raw_return_gte=0.1
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="exactly one"):
        await service.execute()
    assert recalls.published is None
