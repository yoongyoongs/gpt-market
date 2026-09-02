"""RT-08：聚合 READ 端点（/intraday/attention, /market/intraday-status,
/pipeline/eod/latest）——JSON 契约 + V3 未启用 503。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v3 import router
from app.container import container

NOW = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)


class _AttentionRepo:
    async def open_events(self, *, codes=None, entry_plan_id=None,
                          event_types=None, limit=100):
        return [{"event_type": "STOP_HIT", "code": "000001"}]


class _OrchRepo:
    async def latest_runs(self, limit=50):
        return [{"job_id": "features", "status": "SUCCEEDED",
                 "idempotency_key": "2026-09-01"}]


class _Uow:
    def __init__(self):
        self.attention = _AttentionRepo()
        self.orchestrator = _OrchRepo()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _V3:
    enabled = True

    def uow(self):
        return _Uow()


class _DisabledV3:
    enabled = False


def _client(monkeypatch, v3):
    monkeypatch.setattr(container, "v3", v3)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_aggregate_read_endpoints(monkeypatch):
    with _client(monkeypatch, _V3()) as client:
        attention = client.get("/api/v3/intraday/attention?limit=10")
        status = client.get("/api/v3/market/intraday-status")
        pipeline = client.get("/api/v3/pipeline/eod/latest")

    assert attention.status_code == 200
    body = attention.json()
    assert body["source"] == "attention-read-v1"
    assert body["count"] == 1
    assert body["known_at"] is not None

    assert status.status_code == 200
    status_body = status.json()
    assert status_body["source"] == "intraday-status-v1"
    assert status_body["session"] in {
        "OPEN", "LUNCH_BREAK", "PRE_OPEN", "CLOSED",
    }
    assert status_body["is_trading_day"] in (True, False)

    assert pipeline.status_code == 200
    pipeline_body = pipeline.json()
    assert pipeline_body["source"] == "pipeline-eod-latest-v1"
    assert pipeline_body["jobs"]["features"]["status"] == "SUCCEEDED"
    assert pipeline_body["overall"] == "COMPLETED"


def test_v3_disabled_returns_503(monkeypatch):
    with _client(monkeypatch, _DisabledV3()) as client:
        for path in (
            "/api/v3/intraday/attention",
            "/api/v3/market/intraday-status",
            "/api/v3/pipeline/eod/latest",
        ):
            assert client.get(path).status_code == 503, path
