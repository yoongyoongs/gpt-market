import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI

from app.api.v3 import router
from app.v3.application.build_context_pack import LEVEL_SETTINGS
from app.v3.domain.context import ContextLevel


def test_phase6_context_budget_ranges_match_architecture():
    assert 2_000 <= LEVEL_SETTINGS[ContextLevel.FAST][0] <= 4_000
    assert 5_000 <= LEVEL_SETTINGS[ContextLevel.NORMAL][0] <= 8_000
    assert 10_000 <= LEVEL_SETTINGS[ContextLevel.DEEP][0] <= 14_000


def test_phase6_routes_remain_read_only_after_later_phases_are_added():
    """API-002：纯读路由保持 get-only；comparison-pack/context-pack 的
    build 有落库副作用，必须是 post（GET 仅 deprecated 兼容）。"""
    app = FastAPI()
    app.include_router(router)
    paths = app.openapi()["paths"]
    owned_paths = {
        "/api/v3/market-overview",
        "/api/v3/candidates/comparison-pack",
        "/api/v3/stocks/{code}/context-pack",
        "/api/v3/stocks/{code}/evidence",
        "/api/v3/context-packs/{context_pack_id}",
        "/api/v3/task-context/{profile}",
        "/api/v3/task-runs",
        "/api/v3/task-runs/{task_run_id}",
    }
    phase6_paths = {path: paths[path] for path in owned_paths}
    assert phase6_paths
    build_paths = {
        "/api/v3/candidates/comparison-pack",
        "/api/v3/stocks/{code}/context-pack",
    }
    for path, operations in phase6_paths.items():
        allowed = {"get", "post"} if path in build_paths else {"get"}
        assert set(operations) <= allowed | {"parameters"}, path
    for path in build_paths:
        assert "post" in paths[path]
        assert paths[path]["get"]["deprecated"] is True


def test_phase6_acceptance_harness_keeps_external_gate_explicit():
    from scripts.v3_phase6_acceptance import percentile

    assert percentile([1, 2, 3, 4, 5], 0.95) == 5


def test_phase6_acceptance_script_can_run_directly_from_another_directory(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "v3_phase6_acceptance.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "V3 Phase 6 isolated acceptance" in result.stdout
