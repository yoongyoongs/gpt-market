from fastapi import FastAPI

from app.api.v3 import router
from app.v3.application.build_context_pack import LEVEL_SETTINGS
from app.v3.domain.context import ContextLevel


def test_phase6_context_budget_ranges_match_architecture():
    assert 2_000 <= LEVEL_SETTINGS[ContextLevel.FAST][0] <= 4_000
    assert 5_000 <= LEVEL_SETTINGS[ContextLevel.NORMAL][0] <= 8_000
    assert 10_000 <= LEVEL_SETTINGS[ContextLevel.DEEP][0] <= 14_000


def test_phase6_routes_are_read_only_and_do_not_expose_phase7_import():
    app = FastAPI()
    app.include_router(router)
    paths = app.openapi()["paths"]
    phase6_paths = {
        path: operations for path, operations in paths.items()
        if path.startswith("/api/v3/")
    }
    assert phase6_paths
    assert all(
        set(operations) <= {"get", "parameters"}
        for operations in phase6_paths.values()
    )
    assert "/api/v3/ai-results/import" not in phase6_paths


def test_phase6_acceptance_harness_keeps_external_gate_explicit():
    from scripts.v3_phase6_acceptance import percentile

    assert percentile([1, 2, 3, 4, 5], 0.95) == 5
