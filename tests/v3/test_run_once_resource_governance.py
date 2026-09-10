"""run_once 资源治理专项测试（设计方案 §33/§35/§36）。

覆盖：P0-01 backfill 并发上限、P0-06 批间让步、P0-03 candidate 只抓
60m、P0-02 minute60 并发上限、P1-01 Resource Snapshot 降级、P1-03
SchedulerBundle 生命周期收口（成功/异常/幂等）。选股算法零触碰——
所有 fake 与 tests/v3 既有算法 fixture 无交集。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.v3.application.backfill_daily_bars import BackfillDailyBarsService
from app.v3.application.deep_market_data import DeepMarketDataService
from app.v3.operations import resource_snapshot
from tests.v3.test_backfill_daily_bars import (
    DynamicProvider,
    FakePublisher,
    FakeUnitOfWork,
    Store,
    service as backfill_service,
    target as backfill_target,
)
from tests.v3.test_ingest_daily_bars import NOW


# ---------------------------------------------------------------------------
# §33 Test 1：backfill 并发上限（20 只股票 / concurrency=4 → active ≤ 4）
# ---------------------------------------------------------------------------


class ConcurrencyTrackingProvider(DynamicProvider):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.active = 0
        self.max_active = 0

    async def fetch(self, code, period, adjust_type, limit):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.002)
            return await super().fetch(code, period, adjust_type, limit)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_backfill_twenty_targets_concurrency_never_exceeds_limit() -> None:
    targets = tuple(backfill_target(f"6{index:05d}") for index in range(20))
    store = Store(targets)
    provider = ConcurrencyTrackingProvider("provider")
    publisher = FakePublisher(store)

    completed = backfill_service(store, provider, publisher).execute.__self__  # noqa: F841
    runner = backfill_service(store, provider, publisher)
    result = await runner.execute(
        minimum_last_bar_date=date(2026, 8, 20), concurrency=4,
    )

    assert result.status.value in {"COMPLETED", "PARTIAL"}
    assert result.processed_count == 20
    assert provider.max_active <= 4, "active fetches must never exceed concurrency=4"


# ---------------------------------------------------------------------------
# §33 Test 2：批间让步（yield_every=8 / 20 只 → sleep 恰 2 次，不真睡）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_yields_after_every_n_completed(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "app.v3.application.backfill_daily_bars.asyncio.sleep", fake_sleep
    )

    targets = tuple(backfill_target(f"6{index:05d}") for index in range(20))
    store = Store(targets)
    provider = DynamicProvider("provider")
    publisher = FakePublisher(store)
    runner = backfill_service(store, provider, publisher)

    await runner.execute(
        minimum_last_bar_date=date(2026, 8, 20),
        concurrency=4, yield_every=8, yield_seconds=0.5,
    )
    # 5 批 ×4 只：批 1+2 累计 8 → sleep；批 3+4 累计 8 → sleep；批 5 只 4 < 8
    assert sleeps == [0.5, 0.5]

    # yield_every=0 关闭
    sleeps.clear()
    store2 = Store(tuple(backfill_target(f"7{index:05d}") for index in range(20)))
    runner2 = backfill_service(store2, DynamicProvider("provider"), FakePublisher(store2))
    await runner2.execute(
        minimum_last_bar_date=date(2026, 8, 20),
        concurrency=4, yield_every=0, yield_seconds=0.5,
    )
    assert sleeps == []

    # 非法参数
    with pytest.raises(ValueError, match="yield_every"):
        await runner2.execute(
            minimum_last_bar_date=date(2026, 8, 20), yield_every=-1,
        )
    with pytest.raises(ValueError, match="yield_seconds"):
        await runner2.execute(
            minimum_last_bar_date=date(2026, 8, 20), yield_seconds=-0.1,
        )


# ---------------------------------------------------------------------------
# §33 Test 3：candidate Deep 只抓 60m（绝无 5m/15m）
# ---------------------------------------------------------------------------


class PeriodRecordingProvider:
    def __init__(self) -> None:
        self.periods: list[str] = []

    async def get_kline(self, code, period, count, adjust=None):
        self.periods.append(period)
        bar = SimpleNamespace(
            timestamp=NOW, provisional=False, open=10.0, high=11.0, low=9.0,
            close=10.5,
        )
        return SimpleNamespace(klines=[bar], stale=False, source="fake", data_timestamp=NOW)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_candidate_deep_service_requests_only_60m() -> None:
    provider = PeriodRecordingProvider()
    service = DeepMarketDataService(provider, source="legacy-provider", periods=("60m",))
    as_of = datetime(2026, 9, 8, 8, 0, tzinfo=timezone.utc)

    structure = await service.get_intraday_structure("600000", as_of=as_of)

    assert set(structure.periods) == {"60m"}
    assert provider.periods == ["60m"]
    assert "5m" not in provider.periods and "15m" not in provider.periods


def test_scheduler_bundle_candidate_deep_periods_is_60m_only() -> None:
    """build_orchestrators 构造的 candidate Deep 服务必须 periods=("60m",)。"""
    from scripts.v3_scheduler import build_orchestrators

    bundle = build_orchestrators("postgresql+asyncpg://invalid")
    try:
        assert bundle.candidate_scan_deep_service is not None
        assert bundle.candidate_scan_deep_service._periods == ("60m",)
    finally:
        asyncio.run(bundle.close())


# ---------------------------------------------------------------------------
# §33 Test 4：minute60 并发上限（Machine Top120 并发受 semaphore 限制）
# ---------------------------------------------------------------------------


class ActiveRecordingDeepService:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def get_intraday_structure(self, code, *, as_of):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls += 1
        try:
            await asyncio.sleep(0.01)
            return SimpleNamespace(
                periods={"60m": {"status": "AVAILABLE", "stale": False,
                                 "quality": "TRUSTED",
                                 "structure": {"trend": "UP"},
                                 "bar_count": 32, "known_at": as_of}},
                known_at=as_of,
            )
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_minute60_fetch_concurrency_never_exceeds_limit() -> None:
    from app.v3.application.scan_universe import UniverseScanOrchestrator

    deep = ActiveRecordingDeepService()
    orchestrator = UniverseScanOrchestrator(deep_service=deep, minute60_concurrency=2)
    as_of = datetime(2026, 9, 8, 8, 0, tzinfo=timezone.utc)
    security_ids = [uuid4() for _ in range(12)]
    stocks_by_id = {
        sid: SimpleNamespace(feature=SimpleNamespace(code=f"60000{i:02d}"))
        for i, sid in enumerate(security_ids)
    }

    facts, stats = await orchestrator._fetch_minute_60(
        security_ids, stocks_by_id, as_of,
    )

    assert deep.calls == 12
    assert deep.max_active <= 2
    assert stats["available"] == 12
    assert len(facts) == 12


# ---------------------------------------------------------------------------
# §35：Resource Snapshot——缺失文件降级 / fake 解析正确 / 绝不抛错
# ---------------------------------------------------------------------------


def test_resource_snapshot_never_raises_and_reports_unavailable(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(resource_snapshot, "_load_averages", lambda: None)
    monkeypatch.setattr(resource_snapshot, "_proc_status_rss_kb", lambda: None)
    monkeypatch.setattr(resource_snapshot, "_read_int_file", _boom)
    monkeypatch.setattr(resource_snapshot, "_CGROUP_CPU_STAT", "Z:/definitely/missing")

    snap = resource_snapshot.snapshot()

    assert snap["available"] is False
    assert snap["load1"] is None and snap["rss_mb"] is None
    assert snap["cgroup_memory_mb"] is None
    assert snap["cgroup_memory_source"] == "unavailable"
    assert snap["reasons"], "null fields must carry a reason"


def test_resource_snapshot_parses_fake_proc_and_cgroup(monkeypatch) -> None:
    monkeypatch.setattr(resource_snapshot, "_load_averages", lambda: (1.25, 0.9, 0.5))
    monkeypatch.setattr(resource_snapshot, "_proc_status_rss_kb", lambda: 262144)  # 256MB
    reads = {
        resource_snapshot._CGROUP_V2_MEMORY_CURRENT: 536870912,   # 512MB
        resource_snapshot._CGROUP_V2_MEMORY_MAX: 2147483648,      # 2GB
    }

    def fake_read(path: str) -> int | None:
        return reads.get(path)

    monkeypatch.setattr(resource_snapshot, "_read_int_file", fake_read)
    monkeypatch.setattr(
        resource_snapshot,
        "_CGROUP_CPU_STAT",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        resource_snapshot, "_CGROUP_CPU_STAT", "Z:/missing/cgroup/cpu.stat",
        raising=False,
    )

    snap = resource_snapshot.snapshot()

    assert snap["available"] is True
    assert snap["load1"] == 1.25 and snap["load15"] == 0.5
    assert snap["rss_mb"] == 256.0
    assert snap["cgroup_memory_mb"] == 512.0
    assert snap["cgroup_memory_max_mb"] == 2048.0
    assert snap["cgroup_memory_source"] == "cgroup-v2"


def test_resource_snapshot_live_call_is_structurally_safe() -> None:
    """当前平台真实调用必须返回结构完整、绝不抛错（Windows/Linux 均可）。"""
    snap = resource_snapshot.snapshot()
    for key in (
        "available", "timestamp", "load1", "rss_mb", "max_rss_mb",
        "cgroup_memory_mb", "cgroup_memory_max_mb", "cgroup_memory_source",
        "cgroup_cpu_stat", "reasons",
    ):
        assert key in snap


# ---------------------------------------------------------------------------
# §36：SchedulerBundle Provider 生命周期——成功/异常/幂等 close
# ---------------------------------------------------------------------------


class CloseCountingProvider:
    def __init__(self, fail: bool = False) -> None:
        self.close_count = 0
        self._fail = fail

    async def close(self) -> None:
        self.close_count += 1
        if self._fail:
            raise RuntimeError("close exploded")


class ResourcelessService:
    """无 close 属性的对象——bundle 必须跳过而非报错。"""


@pytest.mark.asyncio
async def test_bundle_closes_each_provider_exactly_once() -> None:
    from scripts.v3_scheduler import SchedulerBundle

    provider_a = CloseCountingProvider()
    provider_b = CloseCountingProvider()
    database = CloseCountingProvider()
    bundle = SchedulerBundle(
        main=object(), maintenance=object(), database=database,
        closeables=(provider_a, ResourcelessService(), provider_b, database),
    )

    await bundle.close()
    await bundle.close()  # 幂等：第二次 close 不再触达任何资源

    assert provider_a.close_count == 1
    assert provider_b.close_count == 1
    assert database.close_count == 1


@pytest.mark.asyncio
async def test_bundle_close_swallows_individual_close_failures() -> None:
    from scripts.v3_scheduler import SchedulerBundle

    good = CloseCountingProvider()
    bad = CloseCountingProvider(fail=True)
    bundle = SchedulerBundle(
        main=object(), maintenance=object(), database=good,
        closeables=(bad, good),
    )

    await bundle.close()  # bad 抛错不影响后续 close，也不外泄

    assert bad.close_count == 1
    assert good.close_count == 1


@pytest.mark.asyncio
async def test_run_once_closes_providers_when_maintenance_fails(
    monkeypatch, tmp_path,
) -> None:
    """run_once 异常路径也必须收口——原实现仅成功路径 close database。"""
    from scripts import v3_scheduler as module

    provider = CloseCountingProvider()
    database = CloseCountingProvider()

    class _FakeCalendarMeta:
        source = "fixture"
        calendar_code = "XSHG"
        coverage_end = date(2026, 12, 31)

    class _FakeCalendar:
        metadata = _FakeCalendarMeta()

        def is_trading_day(self, value):
            return False  # 非交易日 → 跳过主链，只走 maintenance

    class _FakeOrchRepo:
        async def latest_succeeded_idempotency_key(self, job_id):
            return None

    class _FakeUow:
        orchestrator = _FakeOrchRepo()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FailingMaintenance:
        async def execute(self, **kwargs):
            raise RuntimeError("maintenance exploded")

    class _FakeResolution:
        def __init__(self, uow_factory, v3_enabled):
            self._uow_factory = uow_factory

        async def resolve(self, environment):
            async with self._uow_factory() as uow:
                await uow.orchestrator.latest_succeeded_idempotency_key("release")
            return {
                "environment": environment,
                "resolved_at": datetime.now(timezone.utc),
                "mode": "V2", "effective_mode": "V2", "reason": "TEST",
                "strategy_version_id": None, "guardrail_version_id": None,
                "configuration": None, "row_version": None,
            }

    class _FakeDatabase:
        sessions = staticmethod(lambda: _FakeUow())

        async def close(self):
            await database.close()

    def fake_build_orchestrators(database_url, release=None, database=None):
        return module.SchedulerBundle(
            main=object(),
            maintenance=_FailingMaintenance(),
            database=_FakeDatabase(),
            closeables=(provider, database),
        )

    monkeypatch.setattr(module, "ExchangeCalendarsAShareCalendar", _FakeCalendar)
    monkeypatch.setattr(module, "SQLAlchemyUnitOfWork", lambda sessions: _FakeUow())
    monkeypatch.setattr(module, "build_database", lambda url: _FakeDatabase())
    monkeypatch.setattr(module, "ReleaseResolver", _FakeResolution)
    monkeypatch.setattr(module, "build_orchestrators", fake_build_orchestrators)
    monkeypatch.setenv("V3_DATABASE_URL", "postgresql+asyncpg://fake")

    with pytest.raises(RuntimeError, match="maintenance exploded"):
        await module.run_once(tmp_path / "report.json")

    assert provider.close_count == 1, "异常路径 Provider 必须被收口"
    assert database.close_count == 1


# ---------------------------------------------------------------------------
# 顺手固化：worker DB pool 预算环境变量命名（P0-05 配置面）
# ---------------------------------------------------------------------------


def test_phase2_defaults_read_resource_governance_values() -> None:
    """P0-01/P0-06 默认值（env 缺省时）= 资源治理安全值。"""
    from scripts.v3_phase2_market_job import build_parser

    parser = build_parser()
    args = parser.parse_args([])

    assert args.concurrency == 4
    assert args.yield_every == 100
    assert args.yield_seconds == 0.5


def test_backfill_service_default_parameters_match_governance() -> None:
    import inspect

    signature = inspect.signature(BackfillDailyBarsService.execute)
    assert signature.parameters["concurrency"].default == 4
    assert signature.parameters["yield_every"].default == 100
    assert signature.parameters["yield_seconds"].default == 0.5
