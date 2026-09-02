"""RT-05：Orchestrator retry / fallback / catch-up（实时方案 §27 RT-05）。

- retry：Job 失败按 max_attempts 重试，attempt 编号如实递增；
- fallback：主 handler 重试耗尽后执行 fallback handler，成功即 SUCCEEDED
  （带 fallback_used 标记并保留主失败原因），下游不因主链抖动被卡死；
- catch-up：调度中断后按交易日补齐错过的主链运行，上限 max_lookback。
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from app.v3.jobs.market_data import catchup_trade_dates
from app.v3.jobs.orchestrator import JobDefinition, Orchestrator


class _FakeOrchRepo:
    def __init__(self):
        self.runs: list[dict] = []

    async def job_lock(self, job_id):
        return None

    async def next_attempt(self, job_id, idempotency_key):
        return sum(
            1 for row in self.runs
            if row["job_id"] == job_id
            and row["idempotency_key"] == idempotency_key
        ) + 1

    async def record(self, **kwargs):
        run_id = uuid4()
        self.runs.append({**kwargs, "job_run_id": run_id})
        return run_id

    async def has_succeeded(self, job_id, idempotency_key):
        return any(
            row["job_id"] == job_id
            and row["idempotency_key"] == idempotency_key
            and row["status"] == "SUCCEEDED"
            for row in self.runs
        )

    async def latest_succeeded_metrics(self, job_id, idempotency_key):
        for row in reversed(self.runs):
            if (row["job_id"] == job_id
                    and row["idempotency_key"] == idempotency_key
                    and row["status"] == "SUCCEEDED"):
                return dict(row.get("metrics") or {})
        return None

    async def latest_succeeded_idempotency_key(self, job_id):
        for row in reversed(self.runs):
            if row["job_id"] == job_id and row["status"] == "SUCCEEDED":
                return row["idempotency_key"]
        return None


class _FakeUow:
    def __init__(self, repo: _FakeOrchRepo):
        self.orchestrator = repo

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _orchestrator(repo: _FakeOrchRepo, jobs) -> Orchestrator:
    return Orchestrator(lambda: _FakeUow(repo), jobs)


TODAY = date(2026, 9, 2)


def _always_fail(job_id: str):
    async def handler(context):
        raise RuntimeError(f"{job_id} upstream broken")
    return handler


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt() -> None:
    repo = _FakeOrchRepo()
    calls = {"n": 0}

    async def flaky(context):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient provider error")
        return {"ok": True}

    orch = _orchestrator(repo, (
        JobDefinition(job_id="flaky", handler=flaky, max_attempts=3),
    ))
    report = await orch.execute(trade_date=TODAY)
    assert report["status"] == "COMPLETED"
    assert report["jobs"][0]["status"] == "SUCCEEDED"
    assert report["jobs"][0]["attempt"] == 2
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_exhausts_attempts_and_records_each() -> None:
    repo = _FakeOrchRepo()
    orch = _orchestrator(repo, (
        JobDefinition(job_id="broken", handler=_always_fail("broken"),
                      max_attempts=2),
    ))
    report = await orch.execute(trade_date=TODAY)
    assert report["status"] == "FAILED"
    assert report["jobs"][0]["status"] == "FAILED"
    assert report["jobs"][0]["attempt"] == 2
    assert len(repo.runs) == 2  # 每次尝试都落库为 Run 记录


@pytest.mark.asyncio
async def test_fallback_recovers_after_primary_exhausts_retries() -> None:
    repo = _FakeOrchRepo()

    async def fallback(context):
        return {"source": "fallback", "stale": True}

    orch = _orchestrator(repo, (
        JobDefinition(job_id="primary", handler=_always_fail("primary"),
                      max_attempts=2, fallback=fallback),
    ))
    report = await orch.execute(trade_date=TODAY)
    job = report["jobs"][0]
    assert report["status"] == "COMPLETED"
    assert job["status"] == "SUCCEEDED"
    assert job["metrics"]["fallback_used"] is True
    assert job["metrics"]["stale"] is True
    # 主失败原因必须保留，绝不静默
    assert "primary" in (job["error_summary"] or "")


@pytest.mark.asyncio
async def test_fallback_failure_still_fails_the_job() -> None:
    repo = _FakeOrchRepo()
    orch = _orchestrator(repo, (
        JobDefinition(job_id="primary", handler=_always_fail("primary"),
                      max_attempts=1, fallback=_always_fail("fallback")),
    ))
    report = await orch.execute(trade_date=TODAY)
    assert report["status"] == "FAILED"
    assert report["jobs"][0]["status"] == "FAILED"


def _weekday_calendar():
    def is_trading_day(day: date) -> bool:
        return day.weekday() < 5
    return is_trading_day


def test_catchup_fills_missed_trading_days() -> None:
    # 2026-09-02 周三；最后成功 8/31 周一 → 9/1、9/2 需补跑
    pending = catchup_trade_dates(
        _weekday_calendar(), last_completed=date(2026, 8, 31), today=TODAY,
    )
    assert pending == (date(2026, 9, 1), date(2026, 9, 2))


def test_catchup_up_to_date_returns_only_today() -> None:
    pending = catchup_trade_dates(
        _weekday_calendar(), last_completed=date(2026, 9, 1), today=TODAY,
    )
    assert pending == (TODAY,)


def test_catchup_without_history_is_bounded_by_max_lookback() -> None:
    pending = catchup_trade_dates(
        _weekday_calendar(), last_completed=None, today=TODAY, max_lookback=10,
    )
    assert len(pending) == 8  # 10 个自然日内 8 个交易日
    assert pending[0] == date(2026, 8, 24)  # 8/23 周日 → 8/24 周一
    assert pending[-1] == TODAY


def test_catchup_skips_weekends() -> None:
    # 9/5 周六、9/6 周日 → 只补 9/4 周五与 9/7 周一
    pending = catchup_trade_dates(
        _weekday_calendar(), last_completed=date(2026, 9, 3),
        today=date(2026, 9, 7),
    )
    assert pending == (date(2026, 9, 4), date(2026, 9, 7))
