"""RT-08：ChatGPT/MCP Read Integration（实时方案 §19/§27 RT-08）。

- 聚合 READ 服务：attention 事件、盘中状态、EOD 流水线最新状态；
- MCP 工具注册：MARKET_READ 组 + PORTFOLIO_READ 组（部署级开关），
  让 ChatGPT 一次会话可完成 扫描→深度→Entry/Position Review。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.v3.application.market_intraday_status import MarketIntradayStatusService
from app.v3.application.pipeline_eod_latest import PipelineEodLatestService
from app.v3.application.read_attention import ReadAttentionEventsService

NOW = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)  # 北京 15:00 收盘边界附近


# ---------- attention read ----------


class _FakeAttentionRepo:
    def __init__(self, events):
        self._events = events

    async def open_events(self, *, codes=None, entry_plan_id=None,
                          event_types=None, limit=100):
        return self._events[:limit]


class _FakeUow:
    def __init__(self, repo):
        self.attention = repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _FakeEvent:
    def model_dump(self, mode=None):
        return {"event_type": "STOP_HIT", "code": "000001", "severity": "CRITICAL"}


@pytest.mark.asyncio
async def test_attention_read_carries_point_in_time_fields() -> None:
    service = ReadAttentionEventsService(
        lambda: _FakeUow(_FakeAttentionRepo([_FakeEvent()])),
        clock=lambda: NOW,
    )
    report = await service.execute(codes=["000001"], limit=10)
    assert report["count"] == 1
    assert report["source"] == "attention-read-v1"
    assert report["known_at"] == NOW
    assert report["events"][0]["event_type"] == "STOP_HIT"


# ---------- market intraday status ----------


@pytest.mark.asyncio
async def test_intraday_status_sessions() -> None:
    def local_clock(hour, minute, weekday=2):
        # 2026-09-02 是周三；用固定 UTC 偏移构造北京时间
        from datetime import timedelta

        return datetime(2026, 9, weekday + 1, hour, minute,
                        tzinfo=timezone(timedelta(hours=8)))

    service = MarketIntradayStatusService(
        clock=lambda: local_clock(10, 0), is_trading_day=lambda day: True,
    )
    report = await service.execute()
    assert report["session"] == "OPEN"
    assert report["is_trading_day"] is True
    assert report["source"] == "intraday-status-v1"

    lunch = MarketIntradayStatusService(
        clock=lambda: local_clock(12, 0), is_trading_day=lambda day: True,
    ).execute_sync()
    assert lunch["session"] == "LUNCH_BREAK"

    weekend = MarketIntradayStatusService(
        clock=lambda: local_clock(10, 0, weekday=5),
        is_trading_day=lambda day: False,
    ).execute_sync()
    assert weekend["session"] == "CLOSED"
    assert weekend["is_trading_day"] is False


# ---------- pipeline eod latest ----------


class _FakeOrchRepo:
    async def latest_runs(self, limit=50):
        return [
            {"job_id": "features", "status": "SUCCEEDED",
             "idempotency_key": "2026-09-01", "known_at": NOW},
            {"job_id": "market-data", "status": "FAILED",
             "idempotency_key": "2026-09-01", "known_at": NOW},
        ]


class _FakePipelineUow:
    def __init__(self, repo):
        self.orchestrator = repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_pipeline_eod_latest_groups_per_job() -> None:
    service = PipelineEodLatestService(
        lambda: _FakePipelineUow(_FakeOrchRepo()), clock=lambda: NOW,
    )
    report = await service.execute()
    jobs = report["jobs"]
    assert jobs["features"]["status"] == "SUCCEEDED"
    assert jobs["market-data"]["status"] == "FAILED"
    assert report["overall"] == "PARTIAL"
    assert report["source"] == "pipeline-eod-latest-v1"


# ---------- MCP 工具注册 ----------


def test_mcp_registers_market_and_portfolio_groups(monkeypatch) -> None:
    fast_mcp_module = pytest.importorskip("app.mcp.v3_tools")

    registered: list[str] = []

    class _StubMcp:
        def tool(self):
            def decorator(func):
                registered.append(func.__name__)
                return func
            return decorator

    class _FakeV3:
        enabled = True

    container = type("C", (), {})()
    container.v3 = _FakeV3()
    fast_mcp_module.register_v3_tools(_StubMcp(), container)
    for name in (
        "v3_market_overview", "v3_market_intraday_status",
        "v3_scan_opportunities", "v3_candidate_comparison",
        "v3_stock_decision_context",
        "v3_stock_intraday_structure", "v3_watchlist", "v3_attention_events",
    ):
        assert name in registered, name
    # PORTFOLIO_READ 组默认关闭（部署级开关），显式开启后注册
    assert "v3_position_context" not in registered
    registered.clear()
    monkeypatch.setenv("V3_MCP_PORTFOLIO_ENABLED", "true")
    fast_mcp_module.register_v3_tools(_StubMcp(), container)
    for name in ("v3_position_context", "v3_position_decision_context"):
        assert name in registered, name


def test_mcp_skips_registration_when_v3_disabled() -> None:
    fast_mcp_module = pytest.importorskip("app.mcp.v3_tools")
    registered: list[str] = []

    class _StubMcp:
        def tool(self):
            def decorator(func):
                registered.append(func.__name__)
                return func
            return decorator

    class _FakeV3:
        enabled = False

    container = type("C", (), {})()
    container.v3 = _FakeV3()
    fast_mcp_module.register_v3_tools(_StubMcp(), container)
    assert registered == []


# ---------- R4-P2-010：MCP candidate_comparison（只读） ----------


def _register_with_funcs(uow_factory):
    fast_mcp_module = pytest.importorskip("app.mcp.v3_tools")
    funcs: dict = {}

    class _StubMcp:
        def tool(self):
            def decorator(func):
                funcs[func.__name__] = func
                return func
            return decorator

    class _FakeV3:
        enabled = True
        # staticmethod：类属性存函数经实例访问会被描述符协议绑定 self
        uow = staticmethod(uow_factory)  # 注册时捕获，必须先注入

    container = type("C", (), {})()
    container.v3 = _FakeV3()
    fast_mcp_module.register_v3_tools(_StubMcp(), container)
    return funcs


class _FakePack:
    def model_dump(self, mode=None):
        return {"comparison_pack_id": "pack-1", "members": []}


class _ComparisonRepo:
    def __init__(self, pack):
        self._pack = pack
        self.calls = []

    async def get(self, pack_id):
        self.calls.append(("get", pack_id))
        return self._pack

    async def latest_for_candidate_set(
        self, candidate_set_id, *, field_profile_version, as_of,
    ):
        self.calls.append(("latest", candidate_set_id))
        return self._pack


class _ComparisonUow:
    def __init__(self, repo):
        self.candidate_comparisons = repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_mcp_candidate_comparison_read_only_paths() -> None:
    """缺参 INVALID_ARGUMENT；坏 UUID 拒绝；按 set 读到 pack AVAILABLE；
    无 pack 时 EMPTY（诚实，不伪造比较结果）。"""
    repo = _ComparisonRepo(_FakePack())
    funcs = _register_with_funcs(lambda: _ComparisonUow(repo))
    tool = funcs["v3_candidate_comparison"]

    missing = await tool()
    assert missing["status"] == "INVALID_ARGUMENT"
    bad = await tool(candidate_set_id="not-a-uuid")
    assert bad["status"] == "INVALID_ARGUMENT"

    report = await tool(
        candidate_set_id="11111111-1111-1111-1111-111111111111",
    )
    assert report["status"] == "AVAILABLE"
    assert report["comparison_pack_id"] == "pack-1"
    assert repo.calls[0][0] == "latest"

    empty_funcs = _register_with_funcs(
        lambda: _ComparisonUow(_ComparisonRepo(None))
    )
    empty = await empty_funcs["v3_candidate_comparison"](
        candidate_set_id="11111111-1111-1111-1111-111111111111",
    )
    assert empty["status"] == "EMPTY"
