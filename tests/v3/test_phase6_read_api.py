from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v3 import router
from app.container import container
from app.v3.domain.context import ContextLevel
from app.v3.domain.task import (
    ExpectedRun,
    TaskGroupCounts,
    TaskProfile,
    TaskRun,
    TaskRunReadPage,
)


NOW = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)


def _facts():
    profile = TaskProfile.build(
        profile_code="POST_MARKET", version=1, schedule="0 16 * * 1-5",
        timezone="Asia/Shanghai", trading_calendar_source="XSHG",
        trading_calendar_version="2026.1", context_level=ContextLevel.NORMAL,
        comparison_first=True, candidate_limit=100, topk_limit=10,
        topk_context_level=ContextLevel.DEEP,
        output_schema={"type": "CandidateComparisonResult"},
        expected_group_count=2, grace_seconds=1800, strategy_version="v1",
    )
    expected = ExpectedRun.build(
        task_profile_id=profile.task_profile_id, task_profile_version=1,
        scheduled_for=NOW, window_end=NOW + timedelta(minutes=30), known_at=NOW,
    )
    run = TaskRun(
        expected_run_id=expected.expected_run_id,
        task_profile_id=profile.task_profile_id, task_profile_version=1,
        counts=TaskGroupCounts(expected=2, successful=0, failed=0, pending=2),
    )
    return profile, expected, run


class _Registry:
    def __init__(self, facts): self.facts = facts
    async def latest_task_context(self, profile): return self.facts
    async def read_task_runs(self, **kwargs):
        return TaskRunReadPage(items=(self.facts[2],))
    async def get_task_run(self, task_run_id):
        return self.facts[2] if task_run_id == self.facts[2].task_run_id else None


class _Contexts:
    async def get(self, context_pack_id): return None


class _Uow:
    def __init__(self, facts):
        self.task_registry = _Registry(facts)
        self.context_packs = _Contexts()
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None


class _V3:
    enabled = True
    def __init__(self, facts): self.facts = facts
    def uow(self): return _Uow(self.facts)


def test_phase6_read_json_contract(monkeypatch):
    facts = _facts()
    monkeypatch.setattr(container, "v3", _V3(facts))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        context = client.get("/api/v3/task-context/POST_MARKET")
        runs = client.get("/api/v3/task-runs")
        run = client.get(f"/api/v3/task-runs/{facts[2].task_run_id}")
        missing = client.get(f"/api/v3/context-packs/{uuid4()}")

    assert context.status_code == 200
    assert context.headers["content-type"].startswith("application/json")
    assert context.json()["semantics"]["expected_run"].endswith(
        "not server AI execution"
    )
    assert runs.status_code == 200 and len(runs.json()["items"]) == 1
    assert run.status_code == 200
    assert run.json()["counts"] == {
        "expected": 2, "successful": 0, "failed": 0, "pending": 2
    }
    assert missing.status_code == 404


def test_openapi_exposes_phase6_read_routes():
    app = FastAPI()
    app.include_router(router)
    paths = app.openapi()["paths"]
    assert {
        "/api/v3/market-overview",
        "/api/v3/candidates/comparison-pack",
        "/api/v3/stocks/{code}/context-pack",
        "/api/v3/stocks/{code}/evidence",
        "/api/v3/context-packs/{context_pack_id}",
        "/api/v3/task-context/{profile}",
        "/api/v3/task-runs",
        "/api/v3/task-runs/{task_run_id}",
    } <= set(paths)
    # API-002：build 有落库副作用 → 正式入口 POST，GET 保留 deprecated 兼容
    build_paths = {
        "/api/v3/candidates/comparison-pack",
        "/api/v3/stocks/{code}/context-pack",
    }
    for path in build_paths:
        assert "post" in paths[path]
        assert paths[path]["get"]["deprecated"] is True
    for path in {
        "/api/v3/market-overview",
        "/api/v3/stocks/{code}/evidence",
        "/api/v3/context-packs/{context_pack_id}",
        "/api/v3/task-context/{profile}",
        "/api/v3/task-runs",
        "/api/v3/task-runs/{task_run_id}",
    }:
        assert set(paths[path]) <= {"get", "parameters"}
