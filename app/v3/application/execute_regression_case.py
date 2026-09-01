"""Regression Case 真执行（RC-06C / PF-002）。

整改方案 §9.3：Regression Case 不能只记录"来源 Replay 是否 BLOCKED"，
必须真执行：

    case input requirements → run replay → evaluate expected invariants
    → PASS/FAIL/BLOCKED → diff

产品边界：
- 来源 Replay BLOCKED（如 601233 缺少原时点资料）时，用例继续 BLOCKED，
  绝不补造数据；
- invariant 只支持可用事实核验的固定白名单；不能核验的 invariant 显式
  UNSUPPORTED 并判 FAIL，绝不静默放行；
- 执行结果 append-only 落库 regression_case_executions。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.v3.application.deterministic_replay import DeterministicReplayService
from app.v3.domain.hashing import canonical_hash
from app.v3.domain.performance import ReplayRunCreate
from app.v3.repositories.errors import RepositoryNotFoundError

SUPPORTED_INVARIANTS = (
    "no_lookahead",
    "server_deterministic_executed",
    "feature_recompute_no_mismatch",
)


class ExecuteRegressionCaseService:
    def __init__(
        self,
        uow_factory: Callable,
        *,
        clock: Callable[[], datetime] | None = None,
        replay_service: DeterministicReplayService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._replay_service = replay_service

    async def execute(self, regression_case_id: UUID) -> dict[str, Any]:
        async with self._uow_factory() as uow:
            case = await uow.performance.get_regression_case(regression_case_id)
            if case is None:
                raise RepositoryNotFoundError("regression case not found")
            replay_service = self._replay_service or DeterministicReplayService(
                self._uow_factory, clock=self._clock,
            )
            requirements = case["input_requirements"]
            replay = await replay_service.execute(ReplayRunCreate(
                strategy_version=case["strategy_version"],
                replay_as_of=case["replay_as_of"],
                bar_revision_ids=tuple(requirements.get("bar_revision_ids", ())),
                evidence_ids=tuple(requirements.get("evidence_ids", ())),
                context_pack_ids=tuple(requirements.get("context_pack_ids", ())),
            ))
            if replay["status"] == "BLOCKED":
                # 601233 语义：缺少原时点资料 → 继续 BLOCKED，绝不补造数据
                status = "BLOCKED"
                blocked_reason = "SOURCE_REPLAY_BLOCKED"
                invariant_results: dict[str, Any] = {}
                diff: dict[str, Any] = {"leakage_checks": replay["leakage_checks"]}
            else:
                status, invariant_results, diff = self._evaluate(case, replay)
            payload = {
                "execution_id": uuid4(),
                "regression_case_id": regression_case_id,
                "status": status,
                "replay_run_id": replay["replay_run_id"],
                "blocked_reason": None if status != "BLOCKED" else blocked_reason,
                "invariant_results": invariant_results,
                "diff": diff,
                "known_at": self._clock(),
            }
            payload["content_hash"] = canonical_hash(payload)
            await uow.performance.record_regression_execution(payload)
            await uow.commit()
        return payload

    def _evaluate(self, case: dict, replay: dict) -> tuple[str, dict, dict]:
        layers = replay["result"]["layers"]
        deterministic = layers.get("server_deterministic", {})
        actuals = {
            "no_lookahead": all(
                item["passed"] for item in replay["leakage_checks"]
            ) if replay["leakage_checks"] else False,
            "server_deterministic_executed": deterministic.get("executed") is True,
            "feature_recompute_no_mismatch": (
                deterministic.get("feature_recompute", {}).get("mismatched") == []
            ),
        }
        invariant_results = {}
        for key, expected in case["expected_invariants"].items():
            if key not in actuals:
                # 不能核验 ≠ 已核验：显式 UNSUPPORTED，判不通过
                invariant_results[key] = {
                    "expected": expected, "actual": None,
                    "passed": False, "reason": "UNSUPPORTED_INVARIANT",
                }
                continue
            actual = actuals[key]
            invariant_results[key] = {
                "expected": expected, "actual": actual,
                "passed": bool(actual) == bool(expected),
            }
        status = (
            "PASS" if all(item["passed"] for item in invariant_results.values())
            else "FAIL"
        )
        diff = {
            "replay_run_id": str(replay["replay_run_id"]),
            "feature_recompute": deterministic.get("feature_recompute", {}),
            "ai_decision_replay": layers.get("ai_decision_replay", {}),
        }
        return status, invariant_results, diff
