from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v3_dashboard import router
from app.container import container
from app.v3.domain.features import FeaturePage


NOW = datetime(2026, 9, 1, 7, 30, tzinfo=timezone.utc)
SCAN_ID = uuid4()
SECURITY_ID = uuid4()


class _Features:
    def __init__(self, page, regime=None):
        self.page = page
        self.regime = regime
        self.query_seen = None

    async def query(self, query):
        self.query_seen = query
        return self.page

    async def latest_regime(self):
        return self.regime


class _Attention:
    async def open_events(self, *, limit=100, **kwargs):
        return []


class _Orchestrator:
    async def latest_runs(self, limit=50):
        return [
            {
                "job_id": "features",
                "idempotency_key": "2026-09-02",
                "status": "SUCCEEDED",
                "attempt": 1,
                "metrics": {},
                "error_summary": None,
                "known_at": NOW,
            }
        ]


class _Scans:
    """§37 扫描区块 fake：latest_run/snapshots/expert_rows/security_names。

    names_calls 记录 security_names 调用次数——设计 §39 要求 Final30+轨迹
    名称合并为一次 IN 批量查询，禁止逐行 N+1（断言 == 1）。
    """

    def __init__(self, run=None, top_rows=(), trace_rows=(), names=None):
        self.run = run or SimpleNamespace(
            scan_run_id=SCAN_ID,
            scan_time=NOW,
            market_date=NOW.date(),
            universe_count=5542,
            eligible_count=5100,
            recall_count=800,
            pareto_count=300,
            machine_count=120,
            deep_count=60,
            final_count=30,
            duration_ms=123_456,
        )
        self._top_rows = list(top_rows)
        self._trace_rows = list(trace_rows)
        self._names = names or {}
        self.names_calls: list[list[UUID]] = []

    async def latest_run(self):
        return self.run

    async def expert_rows(self, scan_run_id, expert=None):
        assert scan_run_id == SCAN_ID
        return []

    async def snapshots(self, scan_run_id, **kwargs):
        assert scan_run_id == SCAN_ID
        if kwargs.get("code"):
            return self._trace_rows
        return self._top_rows

    async def security_names(self, security_ids):
        self.names_calls.append(list(security_ids))
        return self._names


def _snap(security_id, code, stage, alive, *, score=1.23, rank=1, drop_reason=None):
    return SimpleNamespace(
        security_id=security_id,
        code=code,
        stage=stage,
        alive=alive,
        score=score,
        rank=rank,
        drop_reason=drop_reason,
    )


class _Uow:
    def __init__(self, features, scans=None):
        self.features = features
        self.attention = _Attention()
        self.orchestrator = _Orchestrator()
        if scans is not None:
            self.scans = scans

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _V3:
    enabled = True

    def __init__(self, features, scans=None):
        self.features = features
        self._scans = scans

    def uow(self):
        return _Uow(self.features, self._scans)


def _app():
    app = FastAPI()
    app.include_router(router)
    return app


def _page():
    return FeaturePage(
        feature_run_id=uuid4(),
        as_of=NOW,
        feature_version="full-market-v1",
        total_count=5551,
        items=(
            {
                "market": "SH",
                "code": "603019",
                "name": "中科曙光<script>",
                "close": 82.6,
                "return_3d": 1.2,
                "return_5d": -0.5,
                "return_20d": 8.1,
                "return_60d": None,
                "position_60d": 0.72,
                "atr_pct": 2.4,
                "amount": 1_230_000_000,
                "volume_ratio_5d": 1.3,
                "coverage": 0.93,
                "stale": False,
                "missing_fields": ["relative_industry_strength"],
            },
        ),
        quality_summary={
            "coverage": 0.998,
            "successful_count": 5542,
            "failed_count": 11,
            "errors": {},
        },
    )


def _regime():
    """§28-§30 Regime fake：分节事实与 stale 徽章。"""
    return SimpleNamespace(
        stale=False,
        stale_reason={},
        breadth={
            "observed": 5542,
            "advancing": 3200,
            "declining": 2100,
            "unchanged": 242,
            "mean_return_3d": 0.0123,
            "advance_decline_ratio": 1.5238,
        },
        turnover={"observed": 5300, "total_amount": 1_520_000_000_000, "coverage": 0.96},
        risk_appetite_facts={
            "stale_count": 42,
            "breakout_20d_count": 130,
            "volume_expansion_count": 500,
        },
    )


def test_dashboard_renders_read_only_feature_facts(monkeypatch):
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features))
    with TestClient(_app()) as client:
        response = client.get(
            "/v3/dashboard?market=SH&sort_by=return_20d&descending=true&limit=20"
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"].startswith("no-store")
    assert "V3 全市场行情特征看板" in response.text
    assert "5,551" in response.text
    assert "筛选后可查询 5,551 条" in response.text
    assert "603019" in response.text
    assert "中科曙光&lt;script&gt;" in response.text
    assert "不是统一评分" in response.text
    # §35/§60 展示层中文化：市场→沪市、缺失字段→相对行业强度（原始值进 title）
    assert "沪市" in response.text
    assert "相对行业强度" in response.text
    assert 'title="relative_industry_strength"' in response.text
    assert features.query_seen.market == "SH"
    assert features.query_seen.limit == 20


def test_dashboard_returns_initializing_without_published_feature_run(monkeypatch):
    monkeypatch.setattr(container, "v3", _V3(_Features(None)))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard")

    assert response.status_code == 503
    assert "准备中" in response.text
    assert "10 秒后自动重试" in response.text


def test_dashboard_is_unavailable_when_v3_is_disabled(monkeypatch):
    disabled = type("DisabledV3", (), {"enabled": False})()
    monkeypatch.setattr(container, "v3", disabled)
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard")

    assert response.status_code == 503
    assert response.json()["detail"] == "V3 is not enabled"


def test_dashboard_rejects_unbounded_limit(monkeypatch):
    monkeypatch.setattr(container, "v3", _V3(_Features(_page())))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard?limit=200")

    assert response.status_code == 422


def test_dashboard_renders_v24_sections(monkeypatch):
    """§31-§34：三区块标题中文化 + Job/状态徽章中文（title 保留原始值）。"""
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard")

    assert response.status_code == 200
    html = response.text
    # §31 Live Status → 交易时段状态；§33 Pipeline → 每日任务运行状态；
    # §34 Attention → 需要关注的市场事件
    assert "交易时段状态" in html
    assert "每日任务运行状态" in html
    assert "需要关注的市场事件" in html
    # §33 Job 中文化（title 保留原始 job_id 便于与 API 对照）
    assert "全市场特征计算" in html
    assert 'title="features"' in html
    # 状态徽章显示中文，title 保留原始状态值（SUCCEEDED）
    assert "成功" in html
    assert 'title="SUCCEEDED"' in html


def test_dashboard_renders_regime_in_chinese(monkeypatch):
    """§28-§30 Regime → 市场整体状态：分节标题/字段中文 + 值格式化。"""
    features = _Features(_page(), regime=_regime())
    monkeypatch.setattr(container, "v3", _V3(features))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard")

    assert response.status_code == 200
    html = response.text
    assert "市场整体状态" in html
    assert "市场宽度" in html
    assert "纳入统计股票数" in html
    assert "上涨家数" in html
    assert "有效成交股票数" in html  # turnover.observed 与 breadth.observed 不同译名
    assert "全市场成交额" in html
    assert "1.52万亿" in html  # total_amount 亿/万亿格式化
    assert "陈旧数据股票数" in html
    assert "20日突破股票数" in html
    # 原始字段名保留在 title（§36 兜底原则的对照口径）
    assert 'title="observed"' in html


def test_dashboard_scan_top_shows_names_and_trace_link(monkeypatch):
    """§38-§39/§55/§91：Final30 加股票名称（批量 1 次查询禁 N+1）+
    “查看筛选轨迹”链接用 trace= 参数。"""
    scans = _Scans(
        top_rows=[_snap(SECURITY_ID, "600030", "FINAL", True, score=0.88, rank=1)],
        names={SECURITY_ID: {"name": "中信证券", "market": "SH", "code": "600030"}},
    )
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features, scans))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard")

    assert response.status_code == 200
    html = response.text
    assert "最终候选 Top30" in html
    assert "中信证券" in html
    assert "查看筛选轨迹" in html
    assert 'href="/v3/dashboard?trace=600030"' in html
    # 分数口径副标题（§56）
    assert "不代表预测涨幅" in html
    # §39 名称合并为一次批量查询（top+trace 共用），禁 N+1
    assert scans.names_calls == [[SECURITY_ID]]


def test_dashboard_trace_query_renders_chinese_trajectory(monkeypatch):
    """§42-§47/§90：trace= 查询任意股票筛选轨迹——标题带名称、
    最终结果行、阶段/淘汰原因中文化、支持行正向说明。"""
    trace_rows = [
        _snap(SECURITY_ID, "600030", "UNIVERSE", True, score=None, rank=None),
        _snap(SECURITY_ID, "600030", "RECALL", False, score=0.4, rank=99,
              drop_reason="no_expert_hit"),
        _snap(SECURITY_ID, "600030", "SAFETY", True, score=None, rank=None,
              drop_reason="support_not_broken"),
    ]
    scans = _Scans(
        top_rows=[],
        trace_rows=trace_rows,
        names={SECURITY_ID: {"name": "中信证券", "market": "SH", "code": "600030"}},
    )
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features, scans))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard?trace=600030")

    assert response.status_code == 200
    html = response.text
    # §46 标题：筛选轨迹 · 名称（代码）
    assert "筛选轨迹 · 中信证券（600030）" in html
    assert "未进入最终候选 Top30" in html
    # §26 阶段中文（title 保留原始 stage）
    assert "多路召回" in html
    assert 'title="RECALL"' in html
    # 状态徽章：已淘汰 / 通过
    assert "已淘汰" in html
    assert "通过" in html
    # §49 淘汰原因中文（title 保留原始 drop_reason）
    assert "未命中任何召回专家" in html
    assert 'title="no_expert_hit"' in html
    # §52 support_not_broken 加分证据 → 正向说明而非"淘汰原因"
    assert "关键支撑仍未跌破（多头证据）" in html


def test_dashboard_trace_accepts_deprecated_whynot_param(monkeypatch):
    """§43：whynot= 旧参数保留兼容（等价 trace=），旧链接不失效。"""
    scans = _Scans(
        trace_rows=[_snap(SECURITY_ID, "600030", "UNIVERSE", True)],
        names={SECURITY_ID: {"name": "中信证券", "market": "SH", "code": "600030"}},
    )
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features, scans))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard?whynot=600030")

    assert response.status_code == 200
    assert "筛选轨迹 · 中信证券（600030）" in response.text


def test_dashboard_trace_without_match_shows_friendly_message(monkeypatch):
    """§44：任意股票可查询；扫描中无记录时给出可读解释而非空白。"""
    scans = _Scans(trace_rows=[], names={})
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features, scans))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard?trace=000001")

    assert response.status_code == 200
    assert "未找到 000001 的记录" in response.text


def test_dashboard_funnel_renders_chinese_stages_and_search_form(monkeypatch):
    """§26/§44：漏斗 chips 中文 + 固定业务顺序 + “为什么没入选”搜索表单
    （隐藏域保留当前筛选参数）。"""
    scans = _Scans(top_rows=[], names={})
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features, scans))
    with TestClient(_app()) as client:
        response = client.get(
            "/v3/dashboard?market=SH&sort_by=return_60d&descending=false&limit=40"
        )

    assert response.status_code == 200
    html = response.text
    assert "候选扫描漏斗" in html
    assert "全市场" in html and "多路召回" in html and "机器精排" in html
    assert 'title="UNIVERSE"' in html and 'title="FINAL"' in html
    assert "查询为什么没入选" in html
    assert '<input name="trace"' in html
    # 隐藏域保留当前筛选（Ajax/刷新后不丢用户上下文）
    assert 'name="market" value="SH"' in html
    assert 'name="sort_by" value="return_60d"' in html
    assert 'name="descending" value="false"' in html
    assert 'name="limit" value="40"' in html


def test_dashboard_pipeline_error_summary_classified(monkeypatch):
    """§80：错误摘要按用户可读类别显示，原始 exception 收进 details。"""
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard")

    assert response.status_code == 200
    assert "每日任务运行状态" in response.text


def test_dashboard_treats_empty_market_as_all(monkeypatch):
    """表单选“全部”市场时 GET 会带上 market=（空串），不得 422（生产实测 2026-09-03）。"""
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features))
    with TestClient(_app()) as client:
        response = client.get(
            "/v3/dashboard?market=&sort_by=return_60d&descending=false&limit=50"
        )

    assert response.status_code == 200
    assert features.query_seen.market is None
    assert features.query_seen.sort_by.value == "return_60d"
    assert features.query_seen.limit == 50


def test_dashboard_treats_empty_limit_as_default(monkeypatch):
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard?market=SH&limit=")

    assert response.status_code == 200
    assert features.query_seen.limit == 50
