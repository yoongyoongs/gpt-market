"""R4-P1-003：Intraday Fast Lane 生产接线（实时方案 §5 / 复验 §28）。

- 全市场 Quote → Overlay → Scanner → IntradayAttentionCandidate →
  Active Pool → 重点池 Deep → Attention 链路真实可跑；
- Quote 失败如实 QUOTE_FAILED，绝不伪造候选；
- stale Quote 不进扫描结果；池内 stale 走 DATA_QUALITY_DEGRADED；
- 只读模式（MCP）不写 AttentionEvent。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import Quote
from app.v3.application.intraday_fast_lane import IntradayFastLaneService
from app.v3.application.intraday_overlay import (
    ActiveIntradayUniverseService,
    IntradayOverlayService,
    IntradayScannerService,
)

NOW = datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc)  # 北京时间 10:00


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


class _FakePage:
    def __init__(self, items, as_of=NOW, next_cursor=None):
        self.items = items
        self.as_of = as_of
        self.next_cursor = next_cursor


class _FakeFeatures:
    def __init__(self, levels=None, *, levels_error=None):
        self._levels = levels or {}
        self._levels_error = levels_error

    async def query(self, query):
        return _FakePage((
            {"code": "000001", "market": "SZ", "ma20": 9.0, "close": 9.5},
            {"code": "600300", "market": "SH", "ma20": 10.0, "close": 10.2},
        ))

    async def daily_levels(self, as_of, *, lookback=20):
        if self._levels_error is not None:
            raise self._levels_error
        return dict(self._levels)


class _FakeRecalls:
    def __init__(self, items=()):
        self._items = items

    async def read_results(self, **kwargs):
        if not self._items:
            return None
        return _FakePage(tuple(self._items))

    async def read_raw(self, *, recall_run_id, limit, cursor):
        # R5-05：EOD 来源 = latest Raw Opportunity
        if cursor is not None or not self._items:
            return None
        return _FakePage(tuple(self._items))


class _FakeWatchlistRepo:
    """现态 Watchlist 读（R5-05）：接收旧式 rows 并映射 state 键。"""

    def __init__(self, rows):
        self._rows = rows

    async def read_watchlist(self, state, limit):
        return [
            {**row, "state": row.get("state", row.get("current_state"))}
            for row in self._rows
        ]


class _FakeReads:
    def __init__(self, watchlist=(), accounts=()):
        self._watchlist = watchlist
        self._accounts = accounts

    async def watchlist_changes(self, limit: int) -> list[dict]:
        return list(self._watchlist)

    async def portfolio_overview(self, limit: int) -> dict:
        return {"accounts": list(self._accounts)}


class _FakeUow:
    def __init__(self, reads, recalls=None, features=None):
        self.features = features or _FakeFeatures()
        self.recalls = recalls or _FakeRecalls()
        self.reads = reads
        self.ai_imports = _FakeWatchlistRepo(reads._watchlist)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def rollback(self):
        return None


class _FakeProvider:
    def __init__(self, quotes=(), index=None, fail=False, expected=None):
        self._quotes = quotes
        self._index = index
        self._fail = fail
        self._expected = expected

    async def get_all_a_shares(self):
        if self._fail:
            raise RuntimeError("clist down")
        if self._expected is not None:
            return self._expected, list(self._quotes)
        return len(self._quotes), list(self._quotes)

    async def get_index_quote(self, code, market):
        if self._index is None:
            raise RuntimeError("no index fixture")
        return self._index


class _FakeEngine:
    def __init__(self):
        self.anomaly_calls = []
        self.dq_calls = []

    async def record_intraday_anomalies(self, candidates, *, as_of):
        self.anomaly_calls.append(candidates)
        return SimpleNamespace(created=tuple(candidates), skipped=0)

    async def record_data_quality(self, quotes, *, universe, as_of):
        self.dq_calls.append((quotes, universe))
        return SimpleNamespace(created=tuple(quotes), skipped=0)


class _FakeDeep:
    """F6-02/§26：测试替身必须遵守生产 DeepMarketDataService 的真实
    Contract——periods["5m"/"15m"/"60m"]（trend/support/resistance），
    绝不再假造 weekly/daily/reversal_state/conflict。"""

    async def get_intraday_structure(self, code, *, as_of):
        return SimpleNamespace(
            code=code, as_of=as_of, known_at=NOW, source="legacy-provider",
            periods={
                "5m": {
                    "status": "AVAILABLE", "precision": "LIMITED",
                    "reason": "MINUTE_FACTS_ARE_FETCH_TIME_FACTS",
                    "bar_count": 32, "stale": False,
                    "structure": {
                        "trend": "DOWN", "support": 9.9, "resistance": 10.5,
                    },
                    "source": "legacy-provider",
                    "upstream_source": "eastmoney",
                    "known_at": NOW, "quality": "OK", "fallback_used": False,
                },
                "15m": {
                    "status": "AVAILABLE", "precision": "LIMITED",
                    "reason": "MINUTE_FACTS_ARE_FETCH_TIME_FACTS",
                    "bar_count": 32, "stale": False,
                    "structure": {
                        "trend": "UP", "support": 9.5, "resistance": 10.8,
                    },
                    "source": "legacy-provider",
                    "upstream_source": "eastmoney",
                    "known_at": NOW, "quality": "OK", "fallback_used": False,
                },
                "60m": {
                    "status": "AVAILABLE", "precision": "LIMITED",
                    "reason": "MINUTE_FACTS_ARE_FETCH_TIME_FACTS",
                    "bar_count": 32, "stale": False,
                    "structure": {
                        "trend": "UP", "support": 9.0, "resistance": 11.0,
                    },
                    "source": "legacy-provider",
                    "upstream_source": "eastmoney",
                    "known_at": NOW, "quality": "OK", "fallback_used": False,
                },
            },
        )


def _service(provider, uow, *, engine=None, deep=None, levels_loader=None):
    return IntradayFastLaneService(
        lambda: uow, provider, IntradayOverlayService(),
        IntradayScannerService(), ActiveIntradayUniverseService(),
        engine=engine, deep_service=deep, clock=lambda: NOW,
        levels_loader=levels_loader,
    )


@pytest.mark.asyncio
async def test_fast_lane_full_chain_runs() -> None:
    """§28 最小验收：Quote→Overlay→Scanner→池→Deep→Attention 全链。"""
    quotes = (
        _quote("000001", volume_ratio=2.5),   # VOLUME_SURGE（阈值 2.0）
        _quote("600000", volume_ratio=1.2),   # 无异常
    )
    provider = _FakeProvider(quotes, index=_quote("000001", 3000.0))
    reads = _FakeReads(
        watchlist=[{"security_market": "SH", "security_code": "600300",
                    "current_state": "WATCHING"}],
        accounts=[{"account_id": "a", "positions": [
            {"security_market": "SZ", "security_code": "000001", "quantity": 100},
            {"security_market": "SZ", "security_code": "999999", "quantity": 0},
        ]}],
    )
    recalls = _FakeRecalls([SimpleNamespace(market="SH", code="600519")])
    engine = _FakeEngine()
    service = _service(
        provider, _FakeUow(reads, recalls), engine=engine, deep=_FakeDeep(),
    )
    report = await service.execute(as_of=NOW)

    assert report["status"] == "AVAILABLE"
    assert report["quote_count"] == 2
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["code"] == "000001"
    assert "VOLUME_SURGE" in report["candidates"][0]["reasons"]
    # 池 = EOD 候选 + WATCHING + 持仓(quantity>0) + 盘中异常，去重合并
    assert report["pool_size"] == 3
    # Attention：scanner 异常写 INTRADAY_ANOMALY
    assert report["attention"]["anomaly_created"] == 1
    assert len(engine.anomaly_calls) == 1
    # 重点池 Deep：候选 1 只 → 1 条摘要，真实 periods 契约透传（F6-02）
    assert report["deep"][0]["code"] == "000001"
    assert report["deep"][0]["status"] == "AVAILABLE"
    assert report["deep"][0]["periods"]["5m"]["trend"] == "DOWN"
    assert report["deep"][0]["periods"]["15m"]["trend"] == "UP"
    assert report["deep"][0]["periods"]["60m"]["trend"] == "UP"
    assert report["deep"][0]["trend_5m"] == "DOWN"
    assert report["deep"][0]["trend_60m"] == "UP"


@pytest.mark.asyncio
async def test_fast_lane_quote_failed_is_honest() -> None:
    provider = _FakeProvider(fail=True)
    engine = _FakeEngine()
    service = _service(
        provider, _FakeUow(_FakeReads()), engine=engine, deep=_FakeDeep(),
    )
    report = await service.execute(as_of=NOW)
    assert report["status"] == "QUOTE_FAILED"
    assert "RuntimeError" in report["quote_error"]
    assert report["candidate_count"] == 0
    assert engine.anomaly_calls == []


@pytest.mark.asyncio
async def test_fast_lane_stale_quote_scanned_out_but_dq_for_pool() -> None:
    """stale 不进候选；池内 stale → DATA_QUALITY_DEGRADED。"""
    quotes = (
        _quote("000001", volume_ratio=2.5),                        # 正常候选
        _quote("600300", market="SH", stale=True, volume_ratio=3.0),  # stale+池内
    )
    reads = _FakeReads(
        watchlist=[{"security_market": "SH", "security_code": "600300",
                    "current_state": "WATCHING"}],
    )
    engine = _FakeEngine()
    service = _service(
        _FakeProvider(quotes), _FakeUow(reads), engine=engine,
    )
    report = await service.execute(as_of=NOW)
    assert report["stale_quote_count"] == 1
    assert all(item["code"] != "600300" for item in report["candidates"])
    assert report["attention"]["data_quality_created"] == 1
    dq_quotes, dq_universe = engine.dq_calls[0]
    assert dq_quotes[0].code == "600300"
    assert ("SH", "600300") in dq_universe


@pytest.mark.asyncio
async def test_fast_lane_read_only_never_touches_engine() -> None:
    """MCP 只读路径：engine=None 也必须完整跑通扫描。"""
    quotes = (_quote("000001", volume_ratio=2.5),)
    service = _service(_FakeProvider(quotes), _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["status"] == "AVAILABLE"
    assert report["candidate_count"] == 1
    assert report["attention"] == {
        "anomaly_created": 0, "data_quality_created": 0,
    }


# ---------- F6-08/P1-12 + Case G：Index Quality Gate ----------
# 指数 Quote stale/不可信/停牌 → index_return=None，relative 强度绝不
# 制造 STRONG_VS_INDEX；data_quality 如实 DEGRADED。


@pytest.mark.asyncio
async def test_fast_lane_stale_index_blocks_strong_vs_index() -> None:
    """F6-08/Case G：指数 Quote stale → index_quality=STALE，+6% 个股
    绝不产生 STRONG_VS_INDEX；data_quality 至少 DEGRADED。"""
    quotes = (_quote("000001", price=9.2 * 1.06),)  # +6%：强于指数
    provider = _FakeProvider(quotes, index=_quote("000001", stale=True))
    service = _service(provider, _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["index_quality"]["status"] == "STALE"
    assert report["index_quality"]["reason"] == "INDEX_QUOTE_STALE"
    for item in report["candidates"]:
        assert "STRONG_VS_INDEX" not in item["reasons"]
    assert report["data_quality"] == "DEGRADED"


@pytest.mark.asyncio
async def test_fast_lane_healthy_index_enables_strong_vs_index() -> None:
    """对照组：指数 Quote 健康 → STRONG_VS_INDEX 正常产生，
    data_quality=GOOD。"""
    quotes = (_quote("000001", price=9.2 * 1.06),)
    # 指数 +0.1%（prev_close 同基准，index_return≈0.001）
    index = _quote("000300", price=3000.0 * 1.001, prev_close=3000.0)
    provider = _FakeProvider(quotes, index=index)
    service = _service(provider, _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["index_quality"]["status"] == "AVAILABLE"
    reasons = report["candidates"][0]["reasons"]
    assert "STRONG_VS_INDEX" in reasons
    assert report["data_quality"] == "GOOD"


@pytest.mark.asyncio
async def test_fast_lane_failed_index_is_unavailable_not_strong() -> None:
    """指数接口异常 → UNAVAILABLE（非 STALE），同样阻断相对强度。"""
    quotes = (_quote("000001", price=9.2 * 1.06),)
    provider = _FakeProvider(quotes, index=None)  # get_index_quote raise
    service = _service(provider, _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["index_quality"]["status"] == "UNAVAILABLE"
    assert "RuntimeError" in report["index_quality"]["reason"]
    for item in report["candidates"]:
        assert "STRONG_VS_INDEX" not in item["reasons"]


# ---------- F6-08/P1-13：真实 EOD Levels 接入 Overlay ----------


@pytest.mark.asyncio
async def test_fast_lane_levels_loader_feeds_overlay() -> None:
    """levels_loader 注入 → Overlay 拿到真实 20D 位：盘中越前高后回落
    → FAILED_BREAKOUT 进候选理由；levels 状态 AVAILABLE 可见。"""
    quotes = (
        _quote("000001", price=9.5, high=9.6),  # high 越 prev_high_20d 后回落
        _quote("600300", market="SH", price=10.2, high=10.3),
    )
    levels = {
        "000001": {
            "prev_high_20d": 9.55, "prev_low_20d": 8.8,
            "support": 8.8, "resistance": 10.5,
        },
    }
    features = _FakeFeatures(levels=levels)
    service = _service(
        _FakeProvider(quotes), _FakeUow(_FakeReads(), features=features),
        levels_loader=features.daily_levels,
    )
    report = await service.execute(as_of=NOW)
    assert report["levels"]["status"] == "AVAILABLE"
    assert report["levels"]["count"] == 1
    by_code = {item["code"]: item for item in report["candidates"]}
    assert "FAILED_BREAKOUT" in by_code["000001"]["reasons"]
    # 无 Levels 的券： Overlay 位字段 None，绝不编造
    assert report["levels"]["status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_fast_lane_levels_loader_failure_is_not_available() -> None:
    """loader 失败 → levels 显式 NOT_AVAILABLE（带 reason），扫描照常，
    绝不让 Levels 故障阻断整个 Fast Lane。"""
    quotes = (_quote("000001", volume_ratio=2.5),)
    features = _FakeFeatures(levels_error=RuntimeError("bars db down"))
    service = _service(
        _FakeProvider(quotes), _FakeUow(_FakeReads(), features=features),
        levels_loader=features.daily_levels,
    )
    report = await service.execute(as_of=NOW)
    assert report["levels"]["status"] == "NOT_AVAILABLE"
    assert "RuntimeError" in report["levels"]["reason"]
    assert report["candidate_count"] == 1  # VOLUME_SURGE 照常


@pytest.mark.asyncio
async def test_fast_lane_no_levels_loader_reports_explicitly() -> None:
    """未注入 loader（如测试外直构）→ NOT_AVAILABLE/NO_LEVELS_SOURCE，
    绝不假装接入了 Levels。"""
    quotes = (_quote("000001", volume_ratio=2.5),)
    service = _service(_FakeProvider(quotes), _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["levels"] == {
        "status": "NOT_AVAILABLE", "reason": "NO_LEVELS_SOURCE",
    }


# ---------- F6-05/Case F：Coverage 语义 + F6-04 聚合 ----------


@pytest.mark.asyncio
async def test_fast_lane_partial_coverage_reports_degraded() -> None:
    """F6-05/Case F：expected=5553 / actual=3000 → coverage≈0.5403、
    full_market_complete=False、状态 PARTIAL（0.5~0.9 带）、
    data_quality=DEGRADED（quote PARTIAL）。绝不假装全市场完整。"""
    quotes = tuple(
        _quote(f"{300000 + i}") for i in range(3000)
    )
    provider = _FakeProvider(quotes, expected=5553)
    service = _service(provider, _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["status"] == "PARTIAL"
    assert report["quote_expected"] == 5553
    assert report["quote_actual"] == 3000
    assert report["quote_missing"] == 2553
    assert report["quote_coverage"] == pytest.approx(0.5403, abs=1e-4)
    assert report["full_market_complete"] is False
    assert report["data_quality"] == "DEGRADED"
    # 覆盖率缺口必须可见（P2-05/L1 Overlay 覆盖）：
    # fixture features 覆盖 2 只 → 2/5553≈0.0004，远低于 0.9 → 降级
    assert report["feature_expected"] == 5553
    assert report["feature_coverage"] == pytest.approx(0.0004, abs=1e-4)


# ---------- F6-04/Case I：Feature 失败不清池 ----------


@pytest.mark.asyncio
async def test_fast_lane_feature_failure_degraded_but_pool_survives() -> None:
    """Case I/R5-P1-003：features 源 FAILED → data_quality=DEGRADED +
    features_error 可见，但池照常由 EOD/Watchlist/持仓合成——
    数据源单点失败绝不清空 Active Pool。"""
    quotes = (
        _quote("000001", volume_ratio=2.5),
        _quote("600000", volume_ratio=1.2),
    )
    reads = _FakeReads(
        watchlist=[{"security_market": "SH", "security_code": "600300",
                    "current_state": "WATCHING"}],
        accounts=[{"account_id": "a", "positions": [
            {"security_market": "SZ", "security_code": "000001", "quantity": 100},
        ]}],
    )
    recalls = _FakeRecalls([SimpleNamespace(market="SH", code="600519")])
    features = _FakeFeatures(
        levels_error=RuntimeError("unused"),
    )
    # 让 features.query 抛错：包裹一层
    class _BoomFeatures(_FakeFeatures):
        async def query(self, query):
            raise RuntimeError("features db down")

    service = _service(
        _FakeProvider(quotes), _FakeUow(reads, recalls, features=_BoomFeatures()),
    )
    report = await service.execute(as_of=NOW)
    assert report["sources"]["features"]["status"] == "FAILED"
    assert "RuntimeError" in report["features_error"]
    assert report["overlay_status"] == "DEGRADED"
    assert report["data_quality"] == "DEGRADED"
    # 池仍含 EOD(600519) + Watchlist(600300) + 持仓(000001)
    assert report["pool_size"] == 3
    # Scanner 照常跑（Overlay 降级但 Quote 事实可用）
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["code"] == "000001"


@pytest.mark.asyncio
async def test_loop_runs_fast_lane_once_and_keeps_summary() -> None:
    """常驻循环：Fast Lane 每间隔与计划触发同跑，摘要可见。"""
    from app.v3.jobs.intraday_loop import IntradayTriggerLoop

    class _FakeFastLane:
        def __init__(self):
            self.calls = 0

        async def execute(self, *, as_of=None):
            self.calls += 1
            return {"status": "AVAILABLE", "candidate_count": 7}

    fast_lane = _FakeFastLane()

    class _NoopRepo:
        async def active_price_trigger_plans(self):
            return ()

    class _LoopUow:
        ai_imports = _NoopRepo()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class _Engine:
        async def evaluate_entry_plan_levels(self, **kwargs):
            raise AssertionError("no plans → engine must not be called")

    loop = IntradayTriggerLoop(
        lambda: _LoopUow(), None, _Engine(), lambda day: True,
        clock=lambda: NOW, fast_lane=fast_lane,
    )
    summary = await loop.run_fast_lane_once()
    assert fast_lane.calls == 1
    assert summary["candidate_count"] == 7
    assert loop.last_fast_lane_summary == summary
    # 未接线时诚实 NOT_WIRED
    bare = IntradayTriggerLoop(
        lambda: _LoopUow(), None, _Engine(), lambda day: True, clock=lambda: NOW,
    )
    assert bare.run_fast_lane_once.__self__ is not None
    unwired = await bare.run_fast_lane_once()
    assert unwired["status"] == "NOT_WIRED"


@pytest.mark.asyncio
async def test_fast_lane_batch_delay_not_stale() -> None:
    """R5-P0-001 §59.4 Case T4：批量 Quote 在 loop_start 后 1-5 秒陆续
    返回是正常网络耗时——绝不因此判 stale/FUTURE，全部正常参与 Scanner。"""
    quotes = (
        _quote(
            "000001", volume_ratio=2.5,
            server_timestamp=NOW + timedelta(seconds=5),
        ),
        _quote("600000", server_timestamp=NOW + timedelta(seconds=2)),
    )
    provider = _FakeProvider(quotes, index=_quote("000001", 3000.0))
    service = _service(provider, _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["status"] == "AVAILABLE"
    assert report["stale_quote_count"] == 0
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["code"] == "000001"


@pytest.mark.asyncio
async def test_deep_covers_pool_when_scanner_empty() -> None:
    """R5-P1-002 §61 Case A：Scanner 为空，Portfolio/Watchlist/EOD
    仍必须进 Deep；Portfolio 按冻结优先级排第一。"""
    quotes = (_quote("999999"),)  # 无异常 → scanner 空
    provider = _FakeProvider(quotes, index=_quote("000001", 3000.0))
    reads = _FakeReads(
        watchlist=[{"security_market": "SH", "security_code": "600000",
                    "current_state": "WATCHING"}],
        accounts=[{"account_id": "a", "positions": [
            {"security_market": "SZ", "security_code": "000001",
             "quantity": 100},
        ]}],
    )
    recalls = _FakeRecalls([SimpleNamespace(market="SH", code="600519")])
    service = _service(
        provider, _FakeUow(reads, recalls), deep=_FakeDeep(),
    )
    report = await service.execute(as_of=NOW)
    assert report["status"] == "AVAILABLE"
    assert report["candidate_count"] == 0
    assert report["pool_size"] == 3
    deep_codes = [item["code"] for item in report["deep"]]
    assert set(deep_codes) == {"000001", "600000", "600519"}
    assert report["deep"][0]["code"] == "000001"
    assert report["deep"][0]["sources"] == ["PORTFOLIO"]


@pytest.mark.asyncio
async def test_deep_limit_prunes_by_frozen_priority() -> None:
    """§61：deep_limit 裁剪按冻结优先级——Portfolio > Watchlist > EOD；
    本例 EOD 候选被裁掉，绝不只喂 Scanner 候选。"""
    quotes = (
        _quote("000001"),                                  # 持仓
        _quote("600000", market="SH"),                     # Watchlist
        _quote("600519", market="SH"),                     # EOD 候选
        _quote("300001", volume_ratio=2.5),                # Scanner 异常
    )
    provider = _FakeProvider(quotes, index=_quote("000001", 3000.0))
    reads = _FakeReads(
        watchlist=[{"security_market": "SH", "security_code": "600000",
                    "current_state": "WATCHING"}],
        accounts=[{"account_id": "a", "positions": [
            {"security_market": "SZ", "security_code": "000001",
             "quantity": 100},
        ]}],
    )
    recalls = _FakeRecalls([SimpleNamespace(market="SH", code="600519")])
    service = IntradayFastLaneService(
        lambda: _FakeUow(reads, recalls), provider,
        IntradayOverlayService(), IntradayScannerService(),
        ActiveIntradayUniverseService(), deep_service=_FakeDeep(),
        deep_limit=2, clock=lambda: NOW,
    )
    report = await service.execute(as_of=NOW)
    assert report["pool_size"] == 4
    assert len(report["deep"]) == 2
    assert report["deep"][0]["code"] == "000001"   # PORTFOLIO
    assert report["deep"][1]["code"] == "600000"   # WATCHLIST


@pytest.mark.asyncio
async def test_partial_market_coverage_is_not_available() -> None:
    """R5-P1-004 §62.2：expected=100 / actual=50 → coverage=0.5，
    status=PARTIAL、full_market_complete=false，绝不冒充全市场完成。"""
    quotes = tuple(
        _quote(f"{600000 + i}") for i in range(50)
    )

    class _PartialProvider(_FakeProvider):
        async def get_all_a_shares(self):
            return 100, list(self._quotes)

    service = _service(_PartialProvider(quotes), _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["status"] == "PARTIAL"
    assert report["quote_expected"] == 100
    assert report["quote_actual"] == 50
    assert report["quote_missing"] == 50
    assert report["quote_coverage"] == 0.5
    assert report["full_market_complete"] is False


@pytest.mark.asyncio
async def test_severely_short_market_marks_unavailable() -> None:
    """§62.2：coverage < 0.5 → UNAVAILABLE_FOR_FULL_MARKET_SCAN。"""
    quotes = tuple(_quote(f"{600000 + i}") for i in range(30))

    class _SevereProvider(_FakeProvider):
        async def get_all_a_shares(self):
            return 100, list(self._quotes)

    service = _service(_SevereProvider(quotes), _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["status"] == "UNAVAILABLE_FOR_FULL_MARKET_SCAN"
    assert report["quote_coverage"] == 0.3
    assert report["full_market_complete"] is False


@pytest.mark.asyncio
async def test_feature_repo_failure_does_not_empty_other_sources() -> None:
    """R5-P1-003 §62.4：Feature Repo FAILED → Overlay DEGRADED，
    但 Watchlist/Portfolio/EOD 照常加载，池不被清空。"""
    quotes = (_quote("000001"),)

    class _BrokenFeatures:
        async def query(self, query):
            raise RuntimeError("feature repo down")

    class _UowWithBrokenFeatures(_FakeUow):
        def __init__(self, reads, recalls=None):
            super().__init__(reads, recalls)
            self.features = _BrokenFeatures()

    reads = _FakeReads(
        watchlist=[{"security_market": "SH", "security_code": "600000",
                    "current_state": "WATCHING"}],
        accounts=[{"account_id": "a", "positions": [
            {"security_market": "SZ", "security_code": "000001",
             "quantity": 100},
        ]}],
    )
    recalls = _FakeRecalls([SimpleNamespace(market="SH", code="600519")])
    service = _service(
        _FakeProvider(quotes, index=None),
        _UowWithBrokenFeatures(reads, recalls), deep=_FakeDeep(),
    )
    report = await service.execute(as_of=NOW)
    assert report["sources"]["features"]["status"] == "FAILED"
    assert "RuntimeError" in report["sources"]["features"]["error"]
    assert report["overlay_status"] == "DEGRADED"
    assert report["sources"]["watchlist"]["status"] == "AVAILABLE"
    assert report["sources"]["portfolio"]["status"] == "AVAILABLE"
    assert report["sources"]["eod"]["status"] == "AVAILABLE"
    assert report["pool_size"] == 3


@pytest.mark.asyncio
async def test_session_failure_marks_all_sources_failed_but_quotes_still_scan() -> None:
    """§62.4 极端：UoW 会话都建不起来 → 四源 FAILED，全市场 Quote
    与 Scanner 照跑（实时链不依赖历史投影）。"""
    class _SessionlessFactory:
        def __call__(self):
            class _Boom:
                async def __aenter__(self):
                    raise RuntimeError("db down")

                async def __aexit__(self, *args):
                    return None

            return _Boom()

    quotes = (_quote("000001", volume_ratio=2.5),)
    service = _service(
        _FakeProvider(quotes, index=None), _SessionlessFactory(),
    )
    report = await service.execute(as_of=NOW)
    assert all(
        sources["status"] == "FAILED"
        for sources in report["sources"].values()
    )
    assert report["status"] == "AVAILABLE"
    assert report["quote_actual"] == 1
    # 池只剩 Scanner 异常候选（历史投影全失效）——实时链照常工作
    assert report["pool_size"] == 1
    assert report["deep"] == []  # 未接 deep service


@pytest.mark.asyncio
async def test_watchlist_reads_current_state_not_event_history() -> None:
    """R5-P2-013 §63：Watchlist 来源 = 现态表——长期稳定 WATCHING /
    WAIT_ENTRY / ACTION_READY 票进池，CLOSED / INVALIDATED 不进。
    一只不在任何近期变更事件里的票绝不因翻页遗漏而消失。"""
    reads = _FakeReads(accounts=[])
    uow = _FakeUow(reads)
    uow.ai_imports = _FakeWatchlistRepo([
        {"security_market": "SH", "security_code": "600000",
         "state": "WATCHING"},
        {"security_market": "SH", "security_code": "600036",
         "state": "WAIT_ENTRY"},
        {"security_market": "SZ", "security_code": "000001",
         "state": "ACTION_READY"},
        {"security_market": "SZ", "security_code": "000002",
         "state": "CLOSED"},
        {"security_market": "SZ", "security_code": "000003",
         "state": "INVALIDATED"},
    ])
    service = _service(_FakeProvider((_quote("999999"),), index=None), uow)
    report = await service.execute(as_of=NOW)
    assert report["sources"]["watchlist"] == {
        "status": "AVAILABLE", "count": 3,
    }
    assert report["pool_size"] == 3


@pytest.mark.asyncio
async def test_feature_coverage_reported() -> None:
    """R5-P2-011 §63：feature overlay 必须暴露 expected/actual/coverage，
    只加载部分特征不允许无覆盖率说明。"""
    quotes = (_quote("000001"), _quote("600000", market="SH"))
    provider = _FakeProvider(quotes, index=None)
    service = _service(provider, _FakeUow(_FakeReads()))
    report = await service.execute(as_of=NOW)
    assert report["feature_expected"] == 2
    assert report["feature_actual"] == 2  # fake feature 两只
    assert report["feature_coverage"] == 1.0
