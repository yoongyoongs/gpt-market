"""RT-10：Performance / Replay / Runtime Closure（实时方案 §27 RT-10）。

- BarsOutcomeProvider：生产级 Recall Observation Outcome Provider，
  从已落库的日 K Revision + 指数基准 Revision 点时计算 future_price /
  benchmark_return，数据不足时显式 UNAVAILABLE，绝不伪造；
- 调度收口：Performance Mature 与 Recall Observation Mature 进入
  维护链自动运行（"Performance Mature 自动计算"审计项）。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.v3.domain.recall import PerformanceObservation, ObservationStatus
from app.v3.providers.bars_outcome import BarsOutcomeProvider

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)


def _observation(matures_at, *, security_id=None, as_of=None, baseline=10.0):
    return PerformanceObservation.build(
        recall_run_id=uuid4(),
        security_id=security_id or uuid4(),
        horizon_sessions=5,
        status=ObservationStatus.PENDING,
        as_of=as_of or datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
        matures_at=matures_at,
        known_at=datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
        baseline_price=baseline,
    )


class _Bar:
    def __init__(self, bar_time, close, high=None, low=None, provisional=False):
        self.bar_time = bar_time
        self.close = close
        self.high = high if high is not None else close * 1.01
        self.low = low if low is not None else close * 0.99
        self.provisional = provisional


class _Revision:
    def __init__(self, bars):
        self.bars = bars


class _BarsRepo:
    def __init__(self, bars_by_security):
        self._bars = bars_by_security

    async def latest_daily_revisions(self, security_ids, *, as_of):
        return [
            _Revision(self._bars.get(security_id, [])) for security_id in security_ids
        ]


class _IndexRepo:
    def __init__(self, bars):
        self._bars = bars

    async def latest(self, benchmark_code, *, as_of):
        return None if self._bars is None else _Revision(self._bars)


class _Uow:
    def __init__(self, bars_repo, index_repo):
        self.bars = bars_repo
        self.index_benchmarks = index_repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


MATURES = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_outcome_provider_resolves_future_price_from_bars() -> None:
    security_id = uuid4()
    bars = [
        _Bar(datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc), 10.0),
        _Bar(datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc), 10.5),
        _Bar(datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc), 11.0),
    ]
    index = [
        _Bar(datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc), 4000.0),
        _Bar(datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc), 4100.0),
    ]

    def uow():
        return _Uow(_BarsRepo({security_id: bars}), _IndexRepo(index))

    provider = BarsOutcomeProvider(uow)
    outcome = (
        await provider.resolve(
            (_observation(MATURES, security_id=security_id),),
            as_of=NOW,
        )
    )[0]
    assert outcome.future_price == 11.0
    assert outcome.benchmark_return == pytest.approx(0.025)
    assert outcome.available


@pytest.mark.asyncio
async def test_outcome_provider_marks_missing_bars_unavailable() -> None:
    security_id = uuid4()

    def uow():
        return _Uow(_BarsRepo({security_id: []}), _IndexRepo([]))

    provider = BarsOutcomeProvider(uow)
    outcome = (
        await provider.resolve(
            (_observation(MATURES, security_id=security_id),),
            as_of=NOW,
        )
    )[0]
    assert not outcome.available
    assert outcome.future_price is None
    assert outcome.unavailable_reason == "NO_BAR_AT_MATURITY"


@pytest.mark.asyncio
async def test_outcome_provider_benchmark_missing_is_honest_none() -> None:
    security_id = uuid4()
    bars = [
        _Bar(datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc), 10.0),
        _Bar(datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc), 11.0),
    ]

    def uow():
        return _Uow(_BarsRepo({security_id: bars}), _IndexRepo(None))

    provider = BarsOutcomeProvider(uow)
    outcome = (
        await provider.resolve(
            (_observation(MATURES, security_id=security_id),),
            as_of=NOW,
        )
    )[0]
    assert outcome.available
    assert outcome.benchmark_return is None


@pytest.mark.asyncio
async def test_outcome_provider_batches_security_reads() -> None:
    """多个 observation 共享 security 时只读一次日 K Revision。"""
    security_id = uuid4()
    bars = [
        _Bar(datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc), 10.0),
        _Bar(datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc), 11.0),
    ]

    class _CountingBarsRepo(_BarsRepo):
        def __init__(self, inner):
            self._inner = inner
            self.calls = 0

        async def latest_daily_revisions(self, security_ids, *, as_of):
            self.calls += 1
            return await self._inner(security_ids, as_of=as_of)

    repo = _CountingBarsRepo(
        lambda ids, *, as_of: _BarsRepo({security_id: bars}).latest_daily_revisions(
            ids,
            as_of=as_of,
        )
    )

    def uow():
        return _Uow(repo, _IndexRepo(None))

    provider = BarsOutcomeProvider(uow)
    outcomes = await provider.resolve(
        (
            _observation(MATURES, security_id=security_id),
            _observation(MATURES, security_id=security_id, baseline=10.0),
        ),
        as_of=NOW,
    )
    assert len(outcomes) == 2
    assert repo.calls == 1


def test_scheduler_maintenance_chain_runs_mature_jobs() -> None:
    module = pytest.importorskip("scripts.v3_scheduler")
    main, maintenance, database = module.build_orchestrators(
        os.getenv("V3_TEST_DATABASE_URL", "postgresql+asyncpg://invalid")
    )
    order = maintenance.execution_order()
    assert set(order) == {
        "corporate-action-match",
        "projection-verify",
        "performance-mature",
        "recall-observation-mature",
        "shadow-observation",  # STR-001：Shadow Runtime 自动观察 Job
    }
    # mature 链不依赖市场数据链，仅维护链内部相对次序有约束
    assert order.index("corporate-action-match") < order.index("projection-verify")
