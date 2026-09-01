"""RC-06C Regression Case 真执行测试。

整改方案 §9.3：case input requirements → run replay → evaluate expected
invariants → PASS/FAIL/BLOCKED → diff。来源 Replay BLOCKED 时用例继续
BLOCKED，绝不补造数据；不能核验的 invariant 显式 UNSUPPORTED，不放行。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.v3.application.execute_regression_case import ExecuteRegressionCaseService
from app.v3.repositories.errors import RepositoryNotFoundError

NOW = datetime(2026, 8, 26, 8, tzinfo=timezone.utc)


class _FakePerformanceRepo:
    def __init__(self, case, replay_report):
        self._case = case
        self._replay_report = replay_report
        self.recorded = []

    async def get_regression_case(self, case_id):
        if self._case is None or self._case["regression_case_id"] != case_id:
            return None
        return self._case

    async def record_regression_execution(self, payload):
        self.recorded.append(payload)
        return payload


class _FakeUow:
    def __init__(self, performance):
        self.performance = performance

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None


class _StubReplayService:
    def __init__(self, report):
        self._report = report
        self.commands = []

    async def execute(self, command):
        self.commands.append(command)
        return self._report


def _case(**overrides):
    case = {
        "regression_case_id": uuid4(),
        "name": "case-601233-pit",
        "strategy_version": "v1",
        "replay_as_of": NOW,
        "input_requirements": {"bar_revision_ids": [str(uuid4())]},
        "expected_invariants": {"no_lookahead": True},
        "source_replay_run_id": None,
        "status": "ACTIVE",
        "blocked_reason": None,
    }
    case.update(overrides)
    return case


def _completed_replay(mismatched=None):
    deterministic = {
        "executed": True,
        "feature_recompute": {
            "recomputed_count": 1, "verified_count": 1,
            "matched_count": 0 if mismatched else 1,
            "mismatched": mismatched or [],
        },
    }
    return {
        "replay_run_id": uuid4(),
        "status": "COMPLETED",
        "leakage_checks": [{"kind": "bars", "passed": True, "reason": None}],
        "result": {
            "executed": True,
            "mode": "POINT_IN_TIME",
            "layers": {
                "server_deterministic": deterministic,
                "ai_decision_replay": {"executed": False},
            },
        },
    }


async def test_pass_case_runs_replay_and_evaluates_invariants() -> None:
    case = _case()
    replay_report = _completed_replay()
    performance = _FakePerformanceRepo(case, replay_report)
    replay = _StubReplayService(replay_report)
    service = ExecuteRegressionCaseService(
        lambda: _FakeUow(performance), replay_service=replay,
    )
    report = await service.execute(case["regression_case_id"])
    assert report["status"] == "PASS"
    assert report["replay_run_id"] == replay_report["replay_run_id"]
    assert report["invariant_results"]["no_lookahead"] == {
        "expected": True, "actual": True, "passed": True,
    }
    # 真执行：input_requirements 真的传进了 replay 命令
    assert replay.commands[0].bar_revision_ids == tuple(
        map(UUID, case["input_requirements"]["bar_revision_ids"])
    )
    assert performance.recorded and performance.recorded[0]["status"] == "PASS"


async def test_blocked_source_replay_keeps_case_blocked() -> None:
    case = _case()
    replay_report = {
        "replay_run_id": uuid4(),
        "status": "BLOCKED",
        "leakage_checks": [{"kind": "bars", "passed": False, "reason": "MISSING_INPUT"}],
        "result": {
            "executed": False,
            "reason": "POINT_IN_TIME_LEAKAGE_OR_MISSING_INPUT",
            "layers": {
                "server_deterministic": {"executed": False, "reason": "GATE_FAILED"},
                "ai_decision_replay": {"executed": False, "reason": "GATE_FAILED"},
            },
        },
    }
    performance = _FakePerformanceRepo(case, replay_report)
    service = ExecuteRegressionCaseService(
        lambda: _FakeUow(performance),
        replay_service=_StubReplayService(replay_report),
    )
    report = await service.execute(case["regression_case_id"])
    assert report["status"] == "BLOCKED"
    assert report["blocked_reason"] == "SOURCE_REPLAY_BLOCKED"
    # 缺少原时点资料：不补造数据，只有 BLOCKED 事实与泄漏检查 diff
    assert report["diff"]["leakage_checks"][0]["reason"] == "MISSING_INPUT"


async def test_failed_invariant_and_unsupported_invariant_do_not_pass() -> None:
    case = _case(expected_invariants={"no_lookahead": True, "custom_rule": True})
    replay_report = _completed_replay(mismatched=[{"field": "close", "recomputed": 11.0, "stored": 12.0}])
    performance = _FakePerformanceRepo(case, replay_report)
    service = ExecuteRegressionCaseService(
        lambda: _FakeUow(performance),
        replay_service=_StubReplayService(replay_report),
    )
    report = await service.execute(case["regression_case_id"])
    assert report["status"] == "FAIL"
    # 不支持的 invariant 不能静默放行
    assert report["invariant_results"]["custom_rule"]["passed"] is False
    assert report["invariant_results"]["custom_rule"]["reason"] == "UNSUPPORTED_INVARIANT"
    # feature 核验 mismatch 反映在 no_lookahead 之外的核验 diff 中
    assert report["diff"]["feature_recompute"]["mismatched"][0]["field"] == "close"


async def test_missing_case_raises() -> None:
    performance = _FakePerformanceRepo(None, None)
    service = ExecuteRegressionCaseService(
        lambda: _FakeUow(performance), replay_service=_StubReplayService({}),
    )
    try:
        await service.execute(uuid4())
    except RepositoryNotFoundError:
        return
    raise AssertionError("expected RepositoryNotFoundError")
