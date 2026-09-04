"""R5-P1-007/§65：Worker heartbeat 跨进程读取——API/Dashboard 进程
从 operational_health_events 聚合状态；连续 3 次 Fast Lane 失败必须
在 HTTP 状态视图里可见 degraded / last_error / consecutive_errors >= 3。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.v3.application.read_worker_heartbeat import ReadWorkerHeartbeatService

NOW = datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc)


def _row(capability, status, observed_at, metadata, component="intraday-worker"):
    return SimpleNamespace(
        health_event_id=uuid4(),
        component=component,
        capability=capability,
        status=status,
        observed_at=observed_at,
        metadata_payload=metadata,
    )


_LOOP_META_HEALTHY = {
    "last_success_at": NOW.isoformat(),
    "consecutive_errors": 0,
    "last_plan_count": 2,
    "quote_expected": 5432,
    "quote_actual": 5432,
    "quote_coverage": 1.0,
    "active_pool_size": 12,
    "candidate_count": 7,
    "deep_count": 2,
    "provider_health": {"eastmoney": {"consecutive_failures": 0}},
}


def _service(rows):
    class _Strategies:
        async def read_health_events(self, component, limit):
            assert component == "intraday-worker"
            return rows

    class _Uow:
        strategies = _Strategies()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return ReadWorkerHeartbeatService(lambda: _Uow(), clock=lambda: NOW)


@pytest.mark.asyncio
async def test_healthy_heartbeat_view() -> None:
    report = await _service([
        _row("intraday-trigger-loop", "HEALTHY", NOW, _LOOP_META_HEALTHY),
    ]).execute()
    view = report["capabilities"]["intraday-trigger-loop"]
    assert report["degraded"] is False
    assert report["consecutive_errors"] == 0
    assert view["degraded"] is False
    assert view["quote_expected"] == 5432
    assert view["quote_coverage"] == 1.0
    assert view["active_pool_size"] == 12
    assert view["candidate_count"] == 7
    assert view["deep_count"] == 2
    assert view["plan_count"] == 2
    assert view["provider_health"] == {"eastmoney": {"consecutive_failures": 0}}


@pytest.mark.asyncio
async def test_three_fast_lane_failures_visible() -> None:
    """§65 验收场景：连续 3 次 Fast Lane 失败（降级心跳 3 条）→
    状态接口可见 degraded / last_error / consecutive_errors >= 3。"""
    rows = []
    for index in range(3):
        rows.append(_row(
            "intraday-trigger-loop", "DEGRADED",
            datetime.fromtimestamp(NOW.timestamp() - index * 30, tz=timezone.utc),
            {
                "consecutive_errors": 3 - index,
                "last_error_type": "RuntimeError",
                "last_fast_lane_error": "RuntimeError: scan down",
                "last_fast_lane_status": "ERROR",
                "quote_expected": 5432, "quote_actual": 0,
                "quote_coverage": 0.0,
            },
        ))
    report = await _service(rows).execute()
    assert report["degraded"] is True
    assert report["consecutive_errors"] >= 3
    assert report["last_error"] == "RuntimeError: scan down"
    view = report["capabilities"]["intraday-trigger-loop"]
    assert view["status"] == "DEGRADED"
    assert view["degraded"] is True
    assert view["consecutive_errors"] == 3
    assert view["last_error_type"] == "RuntimeError"
    assert view["quote_coverage"] == 0.0


@pytest.mark.asyncio
async def test_latest_per_capability_and_overall() -> None:
    """多 capability 各取最新一条（observed_at desc 流里首个即最新）；
    overall degraded = 任一 capability 降级。"""
    rows = [
        _row("intraday-trigger-loop", "DEGRADED", NOW, {
            "consecutive_errors": 3, "last_error_type": "RuntimeError",
        }),
        _row("intraday-trigger-loop", "HEALTHY", NOW.replace(minute=0), {
            "consecutive_errors": 0,
        }),
        _row("intraday-evidence-poll", "HEALTHY", NOW, {"consecutive_errors": 0}),
    ]
    report = await _service(rows).execute()
    assert set(report["capabilities"]) == {
        "intraday-trigger-loop", "intraday-evidence-poll",
    }
    # 旧的健康心跳被最新降级心跳覆盖
    assert report["capabilities"]["intraday-trigger-loop"]["status"] == "DEGRADED"
    assert report["degraded"] is True
    assert report["capabilities"]["intraday-evidence-poll"]["degraded"] is False
    assert report["latest"]["capability"] == "intraday-trigger-loop"


@pytest.mark.asyncio
async def test_empty_history_is_explicit() -> None:
    report = await _service([]).execute()
    assert report["capabilities"] == {}
    assert report["degraded"] is False
    assert report["consecutive_errors"] == 0
    assert report["last_error"] is None


# ---------- §65 HTTP 验收：状态接口可见 degraded/last_error/连续错误 ----------


def _http_client(monkeypatch, rows):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v3 import router
    from app.container import container

    class _V3:
        enabled = True

        def uow(self):
            class _Strategies:
                async def read_health_events(self, component, limit):
                    return rows

            class _Uow:
                strategies = _Strategies()

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            return _Uow()

    # monkeypatch 保证请求期间替换生效、用后自动还原
    monkeypatch.setattr(container, "v3", _V3())
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_http_status_endpoint_shows_three_fast_lane_failures(monkeypatch) -> None:
    """§65：连续 3 次 Fast Lane 失败 → HTTP GET /operations/worker-heartbeat
    可见 degraded=true / last_error / consecutive_errors >= 3。"""
    rows = [
        _row("intraday-trigger-loop", "DEGRADED", NOW, {
            "consecutive_errors": 3, "last_error_type": "RuntimeError",
            "last_fast_lane_error": "RuntimeError: scan down",
            "last_fast_lane_status": "ERROR",
            "quote_expected": 5432, "quote_actual": 0, "quote_coverage": 0.0,
            "active_pool_size": 12, "candidate_count": 0, "deep_count": 0,
            "last_plan_count": 2,
        }),
    ]
    client = _http_client(monkeypatch, rows)
    response = client.get("/api/v3/operations/worker-heartbeat")
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["consecutive_errors"] >= 3
    assert body["last_error"] == "RuntimeError: scan down"
    view = body["capabilities"]["intraday-trigger-loop"]
    assert view["quote_coverage"] == 0.0
    assert view["active_pool_size"] == 12


def test_http_healthy_worker_not_degraded(monkeypatch) -> None:
    client = _http_client(monkeypatch, [
        _row("intraday-trigger-loop", "HEALTHY", NOW, _LOOP_META_HEALTHY),
    ])
    response = client.get("/api/v3/operations/worker-heartbeat")
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is False
    assert body["consecutive_errors"] == 0
