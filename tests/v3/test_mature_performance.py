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
from app.v3.domain.performance import PerformanceAbility
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

    async def attribution_id_exists(self, attribution_id):
        return any(item.attribution_id == attribution_id for item in self.written)

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

    # 每个成熟 horizon 产出 4 类：SELECTION + INITIAL_ENTRY + USER_EXECUTION
    # + RISK_CONTROL（计划有止损且有成交）；T+20 未满待成熟
    assert report["matured_count"] == 16
    assert report["pending_count"] == 1
    assert report["by_ability"] == {
        "SELECTION": 4, "INITIAL_ENTRY": 4,
        "USER_EXECUTION": 4, "RISK_CONTROL": 4,
    }
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
    assert len(uow.performance.written) == 16


@pytest.mark.asyncio
async def test_trade_anchored_abilities_are_produced():
    """PF-001：INITIAL_ENTRY/USER_EXECUTION/RISK_CONTROL 从真实 Trade/Plan 事实
    自动产出，subject 与绑定语义正确。"""
    security_id = uuid4()
    decision = _decision(security_id)
    revision = _bars(security_id, datetime(2026, 8, 1, tzinfo=timezone.utc), 25)
    trade = _trade(
        decision["decision_id"], security_id,
        datetime(2026, 8, 13, 1, 30, tzinfo=timezone.utc),
    )
    trade["entry_plan_id"] = decision["original_entry_plan_id"]
    trade["entry_plan_version"] = 1
    uow = _FakeUow([decision], [trade], {security_id: revision}, _benchmark())
    service = MaturePerformanceService(lambda: uow, clock=lambda: NOW)
    report = await service.execute(as_of=NOW)

    assert report["by_ability"] == {
        "SELECTION": 4, "INITIAL_ENTRY": 4,
        "USER_EXECUTION": 4, "RISK_CONTROL": 4,
    }
    by_ability = {item.ability: item for item in uow.performance.written}

    entry = by_ability[PerformanceAbility.INITIAL_ENTRY]
    assert entry.subject_type == "DECISION"
    assert entry.trade_id == trade["trade_id"]
    # 08-13 01:30 ∈ 计划入场窗口 [08-12, 08-14]
    assert entry.metrics["entry_within_window"] is True
    # 10.1 ∈ 计划价格区间 [10, 10.2]
    assert entry.metrics["entry_price_in_range"] is True
    assert entry.metrics["plan_binding"] == "BOUND"
    assert entry.trade_bound_entry_plan_id == decision["original_entry_plan_id"]

    execution = by_ability[PerformanceAbility.USER_EXECUTION]
    assert execution.subject_type == "TRADE"
    assert execution.subject_id == trade["trade_id"]
    assert execution.trade_id == trade["trade_id"]
    # 成交日 08-13：open 11.2 / low 11.0 / high 11.4 → 10.1 不在日内区间
    assert execution.metrics["in_day_range"] is False
    assert execution.metrics["price_vs_open_pct"] == pytest.approx(
        10.1 / 11.2 - 1, abs=1e-9
    )

    risk = by_ability[PerformanceAbility.RISK_CONTROL]
    assert risk.stop_hit is False  # 窗口 low ≥ 11.0 > 止损 9.5
    assert risk.metrics["stop_hit"] is False
    assert "risk_outcome" not in risk.metrics  # 未触发 → 无退出判定
    assert risk.mae == pytest.approx(11.0 / 11.1 - 1, abs=1e-9)

    # 幂等：四类 attribution 重跑全部 skip
    rerun = await service.execute(as_of=NOW)
    assert rerun["matured_count"] == 0
    assert len(uow.performance.written) == 16


@pytest.mark.asyncio
async def test_risk_control_records_exit_fact_after_stop_hit():
    """RISK_CONTROL：止损触发后必须陈述是否实际退出（事实，非建议）。"""
    security_id = uuid4()
    decision = _decision(security_id)
    decision["original_entry_plan_snapshot"]["plan"]["stop_loss"] = "11.05"
    revision = _bars(security_id, datetime(2026, 8, 1, tzinfo=timezone.utc), 25)
    buy = _trade(
        decision["decision_id"], security_id,
        datetime(2026, 8, 13, 1, 30, tzinfo=timezone.utc),
    )
    sell = _trade(
        decision["decision_id"], security_id,
        datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc), price="10.9",
    )
    sell["side"] = "SELL"
    uow = _FakeUow(
        [decision], [buy, sell], {security_id: revision}, _benchmark()
    )
    service = MaturePerformanceService(lambda: uow, clock=lambda: NOW)
    await service.execute(as_of=NOW)

    assert uow.performance.written
    risks = [
        item for item in uow.performance.written
        if item.ability is PerformanceAbility.RISK_CONTROL
    ]
    assert len(risks) == 4  # 每个成熟 horizon 一条
    first = next(item for item in risks if item.horizon_sessions == 1)
    assert first.stop_hit is True  # T+1 low 11.0 <= 11.05
    assert first.metrics["first_stop_hit_time"] == (
        datetime(2026, 8, 13, tzinfo=timezone.utc).isoformat()
    )
    assert first.metrics["exited_after_stop"] is True  # 08-14 SELL ≥ 止损日
    assert first.metrics["risk_outcome"] == "EXITED_AFTER_STOP"


@pytest.mark.asyncio
async def test_no_baseline_bar_is_isolated_not_fatal():
    """NEW-PF-001：Revision 存在但无 baseline bar → 单候选跳过 + reason，
    其余候选照常成熟，绝不让整批 Job 失败。"""
    bad_sid = uuid4()
    bad = _decision(bad_sid)
    bad["as_of"] = datetime(2026, 7, 20, tzinfo=timezone.utc)  # 早于全部 bar
    good_sid = uuid4()
    good = _decision(good_sid)
    uow = _FakeUow(
        [bad, good], [],
        {
            bad_sid: _bars(bad_sid, datetime(2026, 8, 1, tzinfo=timezone.utc), 25),
            good_sid: _bars(good_sid, datetime(2026, 8, 1, tzinfo=timezone.utc), 25),
        },
        None,
    )
    service = MaturePerformanceService(lambda: uow, clock=lambda: NOW)
    report = await service.execute(as_of=NOW)

    assert report["status"] == "COMPLETED"
    assert report["issues"] == {"NO_BASELINE_BAR": 1}
    assert report["skipped_count"] == 5  # 坏候选 5 个 horizon 全部跳过
    assert report["matured_count"] == 4  # 好候选照常产出 SELECTION


@pytest.mark.asyncio
async def test_broken_candidate_is_isolated_not_fatal():
    """NEW-PF-001：脏数据候选抛异常 → CANDIDATE_ERROR 隔离，不拖垮整批。"""
    bad_sid = uuid4()
    bad = _decision(bad_sid)
    bad["original_entry_plan_snapshot"] = None  # 脏数据 → AttributeError
    good_sid = uuid4()
    good = _decision(good_sid)
    uow = _FakeUow(
        [bad, good], [],
        {
            bad_sid: _bars(bad_sid, datetime(2026, 8, 1, tzinfo=timezone.utc), 25),
            good_sid: _bars(good_sid, datetime(2026, 8, 1, tzinfo=timezone.utc), 25),
        },
        None,
    )
    service = MaturePerformanceService(lambda: uow, clock=lambda: NOW)
    report = await service.execute(as_of=NOW)

    assert report["status"] == "COMPLETED"
    assert report["issues"] == {"CANDIDATE_ERROR:AttributeError": 1}
    assert report["skipped_count"] == 5
    assert report["matured_count"] == 4


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
