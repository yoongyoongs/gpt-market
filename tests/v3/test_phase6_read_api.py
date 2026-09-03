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
    # API-002/R3-P0-003：build 有落库副作用 → POST；同名 GET 为纯读
    # （读已有 pack，无 deprecated 标记——它是合法读端点）
    build_paths = {
        "/api/v3/candidates/comparison-pack",
        "/api/v3/stocks/{code}/context-pack",
    }
    for path in build_paths:
        assert "post" in paths[path]
        assert "get" in paths[path]
        assert paths[path]["get"].get("deprecated") is not True
    for path in {
        "/api/v3/market-overview",
        "/api/v3/stocks/{code}/evidence",
        "/api/v3/context-packs/{context_pack_id}",
        "/api/v3/task-context/{profile}",
        "/api/v3/task-runs",
        "/api/v3/task-runs/{task_run_id}",
    }:
        assert set(paths[path]) <= {"get", "parameters"}


# --- API-002/R3-P0-003：GET 必须纯读（绝不触发 Build/Publish 落库） ---


class _ReadOnlyComparisonRepo:
    """只暴露读方法——若 GET 误触 build 路径会 AttributeError→500，
    结构性保证纯读语义可测。"""

    def __init__(self, pack):
        self._pack = pack
        self.get_ids = []
        self.latest_kwargs = []

    async def get(self, comparison_pack_id):
        self.get_ids.append(comparison_pack_id)
        return self._pack

    async def latest_for_candidate_set(
        self, candidate_set_id, *, field_profile_version, as_of,
    ):
        self.latest_kwargs.append({
            "candidate_set_id": candidate_set_id,
            "field_profile_version": field_profile_version,
            "as_of": as_of,
        })
        return self._pack


class _ReadOnlyContextRepo:
    def __init__(self, pack):
        self._pack = pack
        self.calls = []

    async def latest_for_subject(self, *, subject_type, subject_id, as_of):
        self.calls.append({
            "subject_type": subject_type, "subject_id": subject_id, "as_of": as_of,
        })
        return self._pack

    async def get(self, context_pack_id):
        return None


class _FixedV3:
    enabled = True

    def __init__(self, uow):
        self._uow = uow

    def uow(self):
        return self._uow


def _client_with_uow(monkeypatch, uow):
    monkeypatch.setattr(container, "v3", _FixedV3(uow))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_comparison_pack_get_is_pure_read(monkeypatch):
    pack = {"comparison_pack_id": "fixture-pack"}
    repo = _ReadOnlyComparisonRepo(pack)
    uow = _Uow(_facts())
    uow.candidate_comparisons = repo
    with _client_with_uow(monkeypatch, uow) as client:
        by_id = client.get(
            f"/api/v3/candidates/comparison-pack?comparison_pack_id={uuid4()}"
        )
        latest = client.get(
            "/api/v3/candidates/comparison-pack"
            f"?candidate_set_id={uuid4()}&field_profile_version=compact-fields.v1"
        )
    assert by_id.status_code == 200 and by_id.json() == pack
    assert latest.status_code == 200 and latest.json() == pack
    assert len(repo.get_ids) == 1
    assert repo.latest_kwargs[0]["field_profile_version"] == "compact-fields.v1"


def test_comparison_pack_get_requires_read_key(monkeypatch):
    uow = _Uow(_facts())
    uow.candidate_comparisons = _ReadOnlyComparisonRepo(None)
    with _client_with_uow(monkeypatch, uow) as client:
        response = client.get("/api/v3/candidates/comparison-pack")
    assert response.status_code == 422
    assert "POST" in response.json()["detail"]


def test_comparison_pack_get_404_when_missing(monkeypatch):
    repo = _ReadOnlyComparisonRepo(None)
    uow = _Uow(_facts())
    uow.candidate_comparisons = repo
    with _client_with_uow(monkeypatch, uow) as client:
        response = client.get(
            f"/api/v3/candidates/comparison-pack?comparison_pack_id={uuid4()}"
        )
    assert response.status_code == 404


def test_stock_context_pack_get_is_pure_read_and_security_scoped(monkeypatch):
    pack = {"context_pack_id": "fixture-pack"}
    repo = _ReadOnlyContextRepo(pack)
    uow = _Uow(_facts())
    uow.context_packs = repo
    with _client_with_uow(monkeypatch, uow) as client:
        found = client.get("/api/v3/stocks/600000/context-pack?market=SH")
    assert found.status_code == 200 and found.json() == pack
    assert repo.calls[0]["subject_type"] == "SECURITY"
    assert repo.calls[0]["subject_id"] == "SH:600000"
    repo._pack = None
    with _client_with_uow(monkeypatch, uow) as client:
        response = client.get("/api/v3/stocks/000001/context-pack?market=SZ")
    assert response.status_code == 404
