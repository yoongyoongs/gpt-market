"""F6-01/§2 + 验收矩阵 Case A/B：统一 V3 Production Runtime Factory。

Case A：Worker / MCP 两条路径复用同一 build_v3_runtime——feature_limit、
deep_limit、Deep 服务、Levels loader 完全同源，唯一差异 = engine
（Worker 可写 Attention，MCP 只读）。绝不允许"Worker 6000 / MCP 2000"
两套能力。

Case B：真实 DeepMarketDataService + 真实 IntradayFastLaneService 组装
（只 Fake 最底层 Provider/UoW）——5m/15m/60m 结构真实推导并透传，
known_at 在 fetch 完成后确定。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import Quote
from app.v3.runtime import V3RuntimeConfig, build_v3_runtime

NOW = datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc)  # 北京时间 10:00


# ---------- Case A：Worker / MCP 同源，唯一差异 = engine ----------


class _FakeUow:
    def __init__(self):
        self.features = SimpleNamespace()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _FakeEngine:
    pass


def test_case_a_worker_and_mcp_runtime_share_capability() -> None:
    provider_manager = object()  # 同一 Provider Fixture：同源验证前提
    worker = build_v3_runtime(
        lambda: _FakeUow(), provider_manager,
        engine=_FakeEngine(), clock=lambda: NOW,
    )
    mcp = build_v3_runtime(
        lambda: _FakeUow(), provider_manager, engine=None, clock=lambda: NOW,
    )
    # 能力参数同源
    assert worker.config == mcp.config
    assert worker.config.feature_limit == 6000
    assert worker.config.deep_limit == 10
    # FastLane 内部能力完全一致
    assert worker.fast_lane._feature_limit == mcp.fast_lane._feature_limit
    assert worker.fast_lane._deep_limit == mcp.fast_lane._deep_limit
    # Deep 服务同构（同 provider、同 source 策略）
    assert (
        worker.deep_service._source == mcp.deep_service._source
        == "legacy-provider"
    )
    assert worker.deep_service._provider is mcp.deep_service._provider
    # Levels loader：两条路径都真实注入（F6-08），绝非 None
    assert worker.fast_lane._levels_loader is not None
    assert mcp.fast_lane._levels_loader is not None
    # 唯一合法差异 = engine（副作用策略）
    assert worker.fast_lane._engine is not None
    assert mcp.fast_lane._engine is None


def test_case_a_env_override_applies_to_both_paths(
    monkeypatch,
) -> None:
    monkeypatch.setenv("V3_FASTLANE_FEATURE_LIMIT", "1234")
    monkeypatch.setenv("V3_FASTLANE_DEEP_LIMIT", "7")
    worker = build_v3_runtime(
        lambda: _FakeUow(), object(), engine=_FakeEngine(), clock=lambda: NOW,
    )
    mcp = build_v3_runtime(
        lambda: _FakeUow(), object(), engine=None, clock=lambda: NOW,
    )
    assert worker.config.feature_limit == 1234
    assert worker.config.deep_limit == 7
    assert worker.fast_lane._feature_limit == mcp.fast_lane._feature_limit
    assert worker.fast_lane._deep_limit == mcp.fast_lane._deep_limit
    assert worker.fast_lane._feature_limit == 1234


def test_case_a_bad_env_falls_back_to_defaults(monkeypatch) -> None:
    monkeypatch.setenv("V3_FASTLANE_FEATURE_LIMIT", "not-a-number")
    config = V3RuntimeConfig.from_env()
    assert config.feature_limit == 6000


# ---------- Case B：真实 Deep + 真实 FastLane，只 Fake 最底层 ----------


def _quote(code: str, price: float = 9.5, **overrides) -> Quote:
    values = dict(
        code=code, name="测试", market="SZ",
        price=price, prev_close=9.20, open=9.21, high=9.60, low=9.18,
        pct_change=3.26, change=0.30, volume=1_234_567, amount=11_500_000.0,
        turnover_rate=2.5, volume_ratio=1.8, amplitude=2.39,
        source="eastmoney", source_timestamp=NOW - timedelta(seconds=3),
        data_timestamp=NOW - timedelta(seconds=3),
        server_timestamp=NOW - timedelta(seconds=1),
        age_seconds=1.0, stale=False, quality="LIVE",
        timestamp_source="eastmoney", snapshot_id="snap-1",
        confidence="HIGH", suspended=False,
    )
    values.update(overrides)
    return Quote(**values)


class _RisingKlineProvider:
    """最底层 Provider 桩：get_kline 返回上升 K 线（→ trend=UP）；
    get_all_a_shares / get_index_quote 服务 FastLane 主链。"""

    def __init__(self, quotes=(), index=None):
        self._quotes = quotes
        self._index = index

    async def get_all_a_shares(self):
        return len(self._quotes), list(self._quotes)

    async def get_index_quote(self, code, market):
        if self._index is None:
            raise RuntimeError("no index fixture")
        return self._index

    async def get_kline(self, code, period, count, adjust="raw"):
        assert period in ("5m", "15m", "60m")
        bars = [
            SimpleNamespace(
                timestamp=NOW - timedelta(minutes=(count - i) * 5),
                close=10.0 + i * 0.1,
                low=9.9 + i * 0.1,
                high=10.1 + i * 0.1,
                provisional=False,
            )
            for i in range(count)
        ]
        return SimpleNamespace(klines=bars, stale=False, source="eastmoney")


class _Page:
    def __init__(self, items):
        self.items = items
        self.as_of = NOW
        self.next_cursor = None
        self.feature_run_id = "run-fixture"


class _FeaturesRepo:
    async def query(self, query):
        return _Page((
            {"code": "000001", "market": "SZ", "ma20": 9.0, "close": 9.5},
        ))

    async def daily_levels(self, as_of, *, lookback=20):
        return {}


class _RecallsRepo:
    async def read_raw(self, *, recall_run_id, limit, cursor):
        return None


class _WatchlistRepo:
    async def read_watchlist(self, state, limit):
        return [{
            "security_market": "SZ", "security_code": "000001",
            "current_state": "WATCHING",
        }]


class _ReadsRepo:
    async def portfolio_overview(self, limit):
        return {"accounts": []}


class _Uow:
    def __init__(self):
        self.features = _FeaturesRepo()
        self.recalls = _RecallsRepo()
        self.ai_imports = _WatchlistRepo()
        self.reads = _ReadsRepo()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_case_b_real_deep_and_fast_lane_assembly() -> None:
    """真实 DeepMarketDataService + 真实 FastLane（只 Fake Provider/
    UoW）：Watchlist 池 → Deep 5m/15m/60m 全部由真实 K 线推导为 UP 并
    透传；known_at 在 fetch 完成后确定（≥ as_of 桩时钟推进值）。"""
    ticks = iter(range(1000))
    clock = lambda: NOW + timedelta(milliseconds=next(ticks))  # noqa: E731
    quotes = (_quote("000001", volume_ratio=2.5),)
    provider = _RisingKlineProvider(
        quotes, index=_quote("000300", price=3003.0, prev_close=3000.0),
    )
    runtime = build_v3_runtime(
        lambda: _Uow(), provider, engine=None, clock=clock,
    )
    report = await runtime.fast_lane.execute(as_of=NOW)

    assert report["status"] == "AVAILABLE"
    assert report["pool_size"] >= 1
    assert report["deep"], "真实 Deep 服务必须产出池内摘要"
    deep = report["deep"][0]
    assert deep["code"] == "000001"
    assert deep["status"] == "AVAILABLE"
    # 真实 _structure 推导：上升 K 线 → UP；support/resistance 为真实
    # min low / max high（绝非桩值）；FastLane periods 视图把结构拍平
    for period in ("5m", "15m", "60m"):
        entry = deep["periods"][period]
        assert entry["status"] == "AVAILABLE"
        assert entry["trend"] == "UP"
        assert entry["support"] == pytest.approx(9.9)
        assert entry["resistance"] == pytest.approx(10.1 + 31 * 0.1)
        # F6-02/§3.2：provenance 真实透传
        assert entry["source"] == "legacy-provider"
        assert entry["upstream_source"] == "eastmoney"
        assert entry["quality"] == "OK"
        assert entry["fallback_used"] is False
        assert entry["known_at"] >= NOW
    # 扁平别名同源
    assert deep["trend_5m"] == "UP"
    assert deep["trend_15m"] == "UP"
    assert deep["trend_60m"] == "UP"
    # F6-07：聚合 known_at = 各周期 known_at 的 max（fetch 后取点）
    period_known = [
        deep["periods"][p]["known_at"] for p in ("5m", "15m", "60m")
    ]
    assert deep["known_at"] >= max(period_known) - timedelta(seconds=0)


# ---------- FC-05：structure_service 出自 Runtime，绝不自建第二套 ----------


def test_case_c_runtime_exposes_structure_service_same_source() -> None:
    """FC-05：Runtime 暴露 structure_service（HTTP Decision Context /
    MCP 的分钟结构服务），bars 源与 runtime.intraday_market_data 是
    同一实例——同 provider 同源，禁止各入口自建第二套。"""
    provider_manager = object()
    worker = build_v3_runtime(
        lambda: _FakeUow(), provider_manager,
        engine=_FakeEngine(), clock=lambda: NOW,
    )
    mcp = build_v3_runtime(
        lambda: _FakeUow(), provider_manager, engine=None, clock=lambda: NOW,
    )
    from app.v3.application.intraday_structure_snapshot import (
        IntradayStructureSnapshotService,
    )

    # 类型正确
    assert isinstance(
        worker.structure_service, IntradayStructureSnapshotService,
    )
    assert isinstance(
        mcp.structure_service, IntradayStructureSnapshotService,
    )
    # bars 源 = 同一 Runtime 的 intraday_market_data 实例（同源硬约束）
    assert (
        worker.structure_service._bars
        is worker.intraday_market_data
    )
    assert (
        mcp.structure_service._bars is mcp.intraday_market_data
    )
    # 同 provider_manager：Worker / MCP / HTTP 三入口能力一致
    assert (
        worker.structure_service._bars._provider
        is mcp.structure_service._bars._provider
    )
    # deep_service 同源：结构服务与 Deep 共享同一 provider
    assert (
        mcp.structure_service._bars._provider
        is mcp.deep_service._provider
    )
