"""RC-06A（PF-001）：Performance Mature Engine —— 系统事实自动计算（离线）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.v3.application.mature_performance import MaturePerformanceService
from app.v3.domain.index_benchmark import (
    IndexBenchmarkBar,
    IndexBenchmarkRevision,
    IndexBenchmarkRevisionContent,
)
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    MarketBar,
    PointInTimePrecision,
)


NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


def _bars(security_id, start, count):
    bars = tuple(
        MarketBar(
            bar_time=start + timedelta(days=index),
            open=10.0 + index * 0.1, high=10.2 + index * 0.1,
            low=9.8 + index * 0.1, close=10.0 + index * 0.1,
            volume=1_000_000, amount=1e7,
            fetch_time=NOW - timedelta(minutes=1),
        )
        for index in range(count)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=security_id, period=BarPeriod.DAY,
        adjust_type=AdjustType.QFQ, source="fixture", upstream_source="fixture",
        raw_bar_available=False,
        point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="fixture QFQ only",
        known_at=NOW - timedelta(minutes=1), bars=bars,
    ))


def _benchmark():
    return IndexBenchmarkRevision.build(IndexBenchmarkRevisionContent(
        revision_id=uuid4(), benchmark_code="HS300", source="fixture",
        upstream_source="fixture",
        fetch_time=NOW - timedelta(minutes=1), known_at=NOW - timedelta(minutes=1),
        bars=tuple(
            IndexBenchmarkBar(
                bar_time=datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=index),
                close=3800.0 + index * 4.0, amount=1e11,
            )
            for index in range(30)
        ),
    ))


def _decision(security_id, *, plan_with_levels=True, direction="LONG"):
    plan = {"entry_price_low": "10", "entry_price_high": "10.2",
            "entry_window_start": "2026-08-12T01:00:00+00:00",
            "entry_window_end": "2026-08-14T07:00:00+00:00"}
    if plan_with_levels:
        plan["stop_loss"] = "9.5"
        plan["take_profit"] = "10.5"
    return {
        "decision_id": uuid4(), "security_id": security_id,
        "as_of": datetime(2026, 8, 12, 7, tzinfo=timezone.utc),
        "produced_at": datetime(2026, 8, 12, 8, tzinfo=timezone.utc),
        "payload": {"direction": direction, "strategy_version": "test-strategy-v1"},
        "original_entry_plan_id": uuid4(),
        "original_entry_plan_snapshot": {"version": 1, "plan": plan},
    }


class _FakePerformanceRepo:
    def __init__(self, decisions, trades):
        self._decisions = decisions
        self._trades = trades
        self.written = []

    async def mature_decision_candidates(self, as_of):
        return [d for d in self._decisions if d["produced_at"] <= as_of]

    async def decision_trades(self, decision_id):
        return [t for t in self._trades if t["decision_id"] == decision_id]

    async def attribution_exists(self, value):
        from app.v3.domain.performance import content_hash
        return any(content_hash(item) == value for item in self.written)

    async def add_attribution(self, command):
        self.written.append(command)
        return command.attribution_id


class _FakeUow:
    def __init__(self, decisions, trades, revision, benchmark):
        self.performance = _FakePerformanceRepo(decisions, trades)
        self._revision = revision
        self._benchmark = benchmark
        self.bars = self
        self.index_benchmarks = self

    async def latest_daily_revisions(self, security_ids, *, as_of):
        return [self._revision[s] for s in security_ids if s in self._revision]

    async def latest(self, benchmark_code, *, as_of):
        return self._benchmark

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None


def _trade(decision_id, security_id, trade_time, price="10.1"):
    return {
        "trade_id": uuid4(), "decision_id": decision_id,
        "security_id": security_id, "side": "BUY",
        "trade_time": trade_time, "price": Decimal(price),
        "quantity": Decimal("1000"), "fee": Decimal("5"),
        "entry_plan_id": None, "entry_plan_version": None,
    }


@pytest.mark.asyncio
async def test_mature_engine_computes_attribution_from_system_facts():
    security_id = uuid4()
    decision = _decision(security_id)
    revision = _bars(security_id, datetime(2026, 8, 1, tzinfo=timezone.utc), 25)
    trade = _trade(
        decision["decision_id"], security_id,
        datetime(2026, 8, 13, 1, 30, tzinfo=timezone.utc),
    )
    uow = _FakeUow([decision], [trade], {security_id: revision}, _benchmark())
    service = MaturePerformanceService(lambda: uow, clock=lambda: NOW)
    report = await service.execute(as_of=NOW)

    assert report["matured_count"] == 4  # T+1/3/5/10 成熟，T+20 未满待成熟
    assert report["pending_count"] > 0
    first = uow.performance.written[0]
    assert first.ability.value == "SELECTION"
    assert first.subject_type == "DECISION"
    assert first.decision_id == decision["decision_id"]
    assert first.horizon_sessions == 1
    # T+1：基线 2026-08-12 收盘 11.1，次日收盘 11.2；基准同窗 3844->3848
    assert first.raw_return == pytest.approx(11.2 / 11.1 - 1, abs=1e-9)
    assert first.excess_return == pytest.approx(
        first.raw_return - (3848.0 / 3844.0 - 1), abs=1e-9
    )
    assert first.mfe == pytest.approx(11.4 / 11.1 - 1, abs=1e-9)
    assert first.mae == pytest.approx(11.0 / 11.1 - 1, abs=1e-9)
    assert first.target_hit is True
    assert first.metrics["actual_trade"] is True
    assert first.metrics["entry_triggered"] is True
    assert first.metrics["direction"] == "LONG"
    assert first.metrics["direction_correctness"] is True
    assert first.metrics["calculation_version"] == "performance-mature-v1"
    # 幂等：重复执行不再写新 Attribution
    second = await service.execute(as_of=NOW)
    assert second["matured_count"] == 0
    assert len(uow.performance.written) == 4


@pytest.mark.asyncio
async def test_missing_inputs_degrade_without_fabrication():
    security_id = uuid4()
    decision = _decision(security_id, plan_with_levels=False, direction=None)
    revision = _bars(security_id, datetime(2026, 8, 1, tzinfo=timezone.utc), 25)
    uow = _FakeUow([decision], [], {security_id: revision}, None)
    service = MaturePerformanceService(lambda: uow, clock=lambda: NOW)
    await service.execute(as_of=NOW)

    assert uow.performance.written
    first = uow.performance.written[0]
    assert first.excess_return is None
    assert first.metrics["excess_reason"] == "BENCHMARK_UNAVAILABLE"
    assert first.target_hit is None
    assert first.stop_hit is None
    assert first.metrics["actual_trade"] is False
    assert first.metrics["entry_triggered"] is False
    assert first.metrics["direction_correctness"] is None
