from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v3_dashboard import router
from app.container import container
from app.v3.domain.features import FeaturePage


NOW = datetime(2026, 9, 1, 7, 30, tzinfo=timezone.utc)


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


class _Uow:
    def __init__(self, features):
        self.features = features
        self.attention = _Attention()
        self.orchestrator = _Orchestrator()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _V3:
    enabled = True

    def __init__(self, features):
        self.features = features

    def uow(self):
        return _Uow(self.features)


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
    assert features.query_seen.market == "SH"
    assert features.query_seen.limit == 20


def test_dashboard_returns_initializing_without_published_feature_run(monkeypatch):
    monkeypatch.setattr(container, "v3", _V3(_Features(None)))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard")

    assert response.status_code == 503
    assert "INITIALIZING" in response.text
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
    """§24：Live Status / EOD Pipeline / Attention 区块 + Regime stale 徽章。"""
    features = _Features(_page())
    monkeypatch.setattr(container, "v3", _V3(features))
    with TestClient(_app()) as client:
        response = client.get("/v3/dashboard")

    assert response.status_code == 200
    html = response.text
    assert "盘中状态（Live Status）" in html
    assert "EOD 流水线（Pipeline）" in html
    assert "Attention 事件（OPEN）" in html
    assert "features" in html and "SUCCEEDED" in html
    # regime stale 徽章（fake regime 未配置 → None → 不出 regime 区块）
    assert "REGIME" in html or "<section" in html
