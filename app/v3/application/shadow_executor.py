"""Shadow Runtime 真执行（RC-07A / STR-001）。

整改方案 §10.2：ShadowObservation 不能只靠调用方提交，必须由 Runtime
对同一 immutable input 真执行：

    同一 (subject_key, as_of) → control executor → treatment executor
    → output hash → 语义 diff → latency/error → ShadowObservation append

产品边界：
- Strategy Executor 只执行服务器能确定性执行的策略部分（Recall/Feature/
  Context/Guardrail 等机器层）；executor 由部署方按 strategy_version_id
  注册，未注册的版本如实记 EXECUTOR_NOT_AVAILABLE，绝不伪造输出；
- AI Decision 层不做 Shadow 重算（SERVER_HAS_NO_MODEL_API）：最终判断仍
  在 ChatGPT Web 完成，历史决策通过 immutable 结果回放核验，绝不把 AI
  判断强行变成 fixed score；
- A/B 实验中 assignment 必须被 Runtime 真消费：分桶结果决定 live 侧，
  ShadowObservation 仍完整记录两侧事实。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.v3.domain.hashing import canonical_hash

AI_DECISION_BOUNDARY = "SERVER_HAS_NO_MODEL_API"

Executor = Callable[[str, datetime], Any]


class ShadowExecutorService:
    def __init__(
        self,
        uow_factory: Callable,
        *,
        executors: dict[UUID, Executor] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._executors = dict(executors or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(
        self, experiment_id: UUID, subject_key: str, *, as_of: datetime | None = None,
    ) -> dict[str, Any]:
        point_in_time = as_of or self._clock()
        async with self._uow_factory() as uow:
            detail = await uow.strategies.experiment_detail(experiment_id)
            experiment = detail["experiment"]
            # Runtime 真消费分桶：assignment 决定 A/B 的 live 侧
            assignment = await uow.strategies.assign_experiment(
                experiment_id, subject_key,
            )
            control = await self._run_side(
                experiment.get("control_strategy_version_id"), subject_key, point_in_time,
            )
            treatment = await self._run_side(
                experiment.get("treatment_strategy_version_id"), subject_key, point_in_time,
            )
            diff = self._semantic_diff(
                control.get("output"), treatment.get("output"),
            )
            materially_divergent = bool(
                diff["paths"]
                or control["error"] is not None
                or treatment["error"] is not None
            )
            divergence_reason = self._divergence_reason(control, treatment, diff)
            error = self._combined_error(control, treatment)
            observation_id = await uow.strategies.add_shadow_observation(
                self._observation(experiment_id, subject_key, control, treatment,
                                  materially_divergent, divergence_reason),
            )
            await uow.commit()
        return {
            "shadow_observation_id": observation_id,
            "experiment_id": experiment_id,
            "experiment_type": experiment["experiment_type"],
            "assignment": assignment,
            "live": self._live_side(assignment),
            "control": self._side_report(control),
            "treatment": self._side_report(treatment),
            "diff": diff,
            "materially_divergent": materially_divergent,
            "divergence_reason": divergence_reason,
            "latency_ms": control["latency_ms"] + treatment["latency_ms"],
            "error": error,
            "ai_decision_layer": {
                "executed": False,
                "boundary": AI_DECISION_BOUNDARY,
                "reason": "AI_DECISION_REQUIRES_EXTERNAL_MODEL",
            },
            "observed_at": point_in_time,
        }

    async def _run_side(
        self, strategy_version_id: UUID | None, subject_key: str, as_of: datetime,
    ) -> dict[str, Any]:
        if strategy_version_id is None:
            return {"strategy_version_id": None, "executed": False,
                    "error": "CONTROL_VERSION_UNDEFINED", "output": None,
                    "latency_ms": 0.0, "hash": self._error_hash(
                        {"error": "CONTROL_VERSION_UNDEFINED"})}
        executor = self._executors.get(strategy_version_id)
        if executor is None:
            # 如实记录不可用，绝不伪造该侧输出
            return {"strategy_version_id": strategy_version_id,
                    "executed": False, "error": "EXECUTOR_NOT_AVAILABLE",
                    "output": None, "latency_ms": 0.0, "hash": self._error_hash(
                        {"error": "EXECUTOR_NOT_AVAILABLE"})}
        started = time.perf_counter()
        try:
            output = await executor(subject_key, as_of)
        except Exception as exc:  # noqa: BLE001 —— 错误事实必须完整落库
            elapsed = (time.perf_counter() - started) * 1000
            return {"strategy_version_id": strategy_version_id,
                    "executed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "output": None, "latency_ms": elapsed,
                    "hash": self._error_hash(
                        {"error": f"{type(exc).__name__}: {exc}"})}
        elapsed = (time.perf_counter() - started) * 1000
        return {"strategy_version_id": strategy_version_id,
                "executed": True, "error": None, "output": output,
                "latency_ms": elapsed, "hash": canonical_hash(output)}

    @staticmethod
    def _error_hash(payload: dict) -> str:
        return canonical_hash(payload)

    @staticmethod
    def _semantic_diff(control: Any, treatment: Any) -> dict[str, Any]:
        paths: list[str] = []
        details: list[dict] = []
        if isinstance(control, dict) and isinstance(treatment, dict):
            for key in sorted(set(control) | set(treatment)):
                if control.get(key) != treatment.get(key):
                    paths.append(key)
                    details.append({"path": key, "control": control.get(key),
                                    "treatment": treatment.get(key)})
        elif control != treatment:
            paths.append("$")
            details.append({"path": "$", "control": control, "treatment": treatment})
        return {"paths": paths, "details": details}

    @staticmethod
    def _divergence_reason(
        control: dict, treatment: dict, diff: dict[str, Any],
    ) -> str | None:
        parts = []
        errors = [side["error"] for side in (control, treatment) if side["error"]]
        if errors:
            parts.append("EXECUTION_ERROR: " + "; ".join(errors))
        if diff["paths"]:
            parts.append("SEMANTIC_FIELD_DIVERGENCE: " + ",".join(diff["paths"][:10]))
        return "; ".join(parts) if parts else None

    @staticmethod
    def _combined_error(control: dict, treatment: dict) -> str | None:
        errors = [side["error"] for side in (control, treatment) if side["error"]]
        return "; ".join(errors) if errors else None

    @staticmethod
    def _observation(
        experiment_id: UUID, subject_key: str, control: dict, treatment: dict,
        materially_divergent: bool, divergence_reason: str | None,
    ):
        from app.v3.domain.strategy import ShadowObservationCreate

        return ShadowObservationCreate(
            shadow_observation_id=uuid4(),
            experiment_id=experiment_id,
            subject_key=subject_key,
            observed_at=datetime.now(timezone.utc),
            control_output_hash=control["hash"],
            treatment_output_hash=treatment["hash"],
            control_payload=(
                control.get("output") if control["executed"]
                else {"error": control["error"]}
            ),
            treatment_payload=(
                treatment.get("output") if treatment["executed"]
                else {"error": treatment["error"]}
            ),
            materially_divergent=materially_divergent,
            divergence_reason=divergence_reason,
            latency_ms=control["latency_ms"] + treatment["latency_ms"],
            error=ShadowExecutorService._combined_error(control, treatment),
        )

    @staticmethod
    def _side_report(side: dict) -> dict[str, Any]:
        return {
            "strategy_version_id": side.get("strategy_version_id"),
            "executed": side["executed"],
            "output_hash": side["hash"],
            "latency_ms": side["latency_ms"],
            "error": side["error"],
        }

    @staticmethod
    def _live_side(assignment: dict) -> dict[str, Any]:
        side = "TREATMENT" if assignment["assignment"] == "TREATMENT" else "CONTROL"
        return {"side": side, "strategy_version_id": assignment["strategy_version_id"]}
