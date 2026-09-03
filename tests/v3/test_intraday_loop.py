from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from uuid import uuid4

import pytest

from app.utils.time import SHANGHAI
from app.v3.jobs.intraday_loop import IntradayTriggerLoop, in_trading_session


NOW = datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc)  # 北京时间 10:00 交易时段


def _plan(stop=None, target=None):
    return {
        "entry_plan_id": uuid4(),
        "decision_id": uuid4(),
        "security_id": uuid4(),
        "code": "002274",
        "market": "SZ",
        "stop_loss": stop,
        "take_profit": target,
        "plan": {},
    }


@dataclass
class _Evaluation:
    created: tuple = ()
    skipped: int = 0


class _FakePlansRepo:
    def __init__(self, plans):
        self._plans = plans

    async def active_price_trigger_plans(self):
        return tuple(self._plans)


class _FakeUow:
    def __init__(self, repo):
        self.ai_imports = repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@dataclass
class _FakeQuote:
    last_price: float | None = 9.5


class _FakeQuoteService:
    def __init__(self, fail_codes: set[str] = frozenset()):
        self.fail_codes = set(fail_codes)
        self.requested: list[str] = []

    async def get_quote_snapshot(self, code: str, *, as_of):
        self.requested.append(code)
        if code in self.fail_codes:
            raise RuntimeError("quote down")
        return _FakeQuote()


class _FakeEngine:
    def __init__(self):
        self.calls: list[dict] = []

    async def evaluate_entry_plan_levels(self, **kwargs):
        self.calls.append(kwargs)
        return _Evaluation(created=(), skipped=1)


def _loop(plans, quote_service=None, engine=None, *, trading_day=True):
    engine = engine or _FakeEngine()
    quote_service = quote_service or _FakeQuoteService()
    return (
        IntradayTriggerLoop(
            lambda: _FakeUow(_FakePlansRepo(plans)),
            quote_service,
            engine,
            lambda value: trading_day,
            clock=lambda: NOW,
        ),
        engine,
        quote_service,
    )


def test_in_trading_session_bounds() -> None:
    assert in_trading_session(time(9, 30)) is True
    assert in_trading_session(time(11, 30)) is True
    assert in_trading_session(time(11, 31)) is False
    assert in_trading_session(time(13, 0)) is True
    assert in_trading_session(time(15, 0)) is True
    assert in_trading_session(time(15, 1)) is False
    assert in_trading_session(time(8, 0)) is False


def test_evaluate_once_triggers_each_plan_and_counts() -> None:
    plans = [_plan(stop=9.0), _plan(stop=8.0, target=12.0)]
    loop, engine, quotes = _loop(plans)
    summary = asyncio.run(loop.evaluate_once())
    assert summary["plan_count"] == 2
    assert summary["evaluated"] == 2
    assert summary["skipped"] == 2  # engine 去抖计数如实透传
    assert len(engine.calls) == 2
    first = engine.calls[0]
    assert first["plan"] == {"stop_loss": 9.0, "take_profit": None}
    assert first["code"] == "002274"
    assert first["market"] == "SZ"
    assert quotes.requested == ["002274", "002274"]


def test_evaluate_once_isolates_quote_and_engine_failures() -> None:
    plans = [_plan(stop=9.0), _plan(stop=9.0), _plan(stop=9.0)]

    class _HalfBrokenEngine(_FakeEngine):
        async def evaluate_entry_plan_levels(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 2:
                raise RuntimeError("engine boom")
            return _Evaluation()

    engine = _HalfBrokenEngine()
    quotes = _FakeQuoteService(fail_codes={"002274"})
    # 三个同码计划：第一次行情失败、第二次引擎失败、第三次成功
    codes = iter(["002274", "000001", "000002"])
    for plan in plans:
        plan["code"] = next(codes)
    loop = IntradayTriggerLoop(
        lambda: _FakeUow(_FakePlansRepo(plans)),
        quotes,
        engine,
        lambda value: True,
        clock=lambda: NOW,
    )
    summary = asyncio.run(loop.evaluate_once())
    assert summary["quote_failed"] == 1
    assert summary["engine_failed"] == 1
    assert summary["evaluated"] == 1


def test_no_plans_is_zero_work() -> None:
    loop, engine, quotes = _loop([])
    summary = asyncio.run(loop.evaluate_once())
    assert summary["plan_count"] == 0
    assert summary["evaluated"] == 0
    assert engine.calls == []
    assert quotes.requested == []


def test_invalid_interval_rejected() -> None:
    with pytest.raises(ValueError):
        IntradayTriggerLoop(
            lambda: None, None, None, lambda value: True, interval_seconds=0,
        )


def test_run_forever_sleeps_outside_session(monkeypatch) -> None:
    """收盘后进入常驻循环：空转等待而非评估（保守频率）。"""
    loop, engine, _ = _loop([_plan(stop=9.0)], trading_day=True)
    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 2:  # 两次空转后退出，避免死循环
            raise asyncio.CancelledError

    monkeypatch.setattr("app.v3.jobs.intraday_loop.asyncio.sleep", _fake_sleep)
    # 北京时间 20:00 → 非时段
    loop._clock = lambda: datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(loop.run_forever())
    assert slept == [60.0, 60.0]
    assert engine.calls == []
