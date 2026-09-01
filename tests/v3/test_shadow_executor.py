"""RC-07A Shadow Runtime 真执行测试（STR-001）。

整改方案 §10.2：同一 immutable input/context → control executor → treatment
executor → output hash → 语义 diff → latency/error → ShadowObservation append。
边界：
- Executor 只覆盖服务器可确定性执行的机器层（Recall/Feature/Context/Guardrail）；
  AI Decision 层不重算（SERVER_HAS_NO_MODEL_API），不把 AI 判断变 fixed score；
- 某一侧没有可用 executor 时如实记 error，绝不伪造结果。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.v3.application.shadow_executor import ShadowExecutorService
from app.v3.domain.hashing import canonical_hash

NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


class _FakeStrategyRepo:
    def __init__(self, experiment, *, running=True):
        self._experiment = experiment
        self._running = running
        self.assign_calls = []
        self.observed = []

    async def experiment_detail(self, experiment_id):
        if self._experiment["experiment_id"] != experiment_id:
            raise AssertionError("unknown experiment")
        return {"experiment": self._experiment, "current_status": "STARTED"}

    async def assign_experiment(self, experiment_id, subject_key):
        self.assign_calls.append((experiment_id, subject_key))
        experiment = self._experiment
        return {
            "experiment_id": experiment_id,
            "subject_key": subject_key,
            "bucket": 7,
            "assignment": "CONTROL",
            "strategy_version_id": experiment["control_strategy_version_id"],
            "shadow_only": experiment["experiment_type"] == "SHADOW",
        }

    async def add_shadow_observation(self, command):
        if not self._running:
            raise RuntimeError("experiment is not running")
        if command.materially_divergent and not command.divergence_reason:
            raise RuntimeError("material divergence requires a reason")
        self.observed.append(command)
        return command.shadow_observation_id


class _FakeUow:
    def __init__(self, strategies):
        self.strategies = strategies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None


def _experiment(**overrides):
    experiment = {
        "experiment_id": uuid4(),
        "experiment_type": "SHADOW",
        "control_strategy_version_id": uuid4(),
        "treatment_strategy_version_id": uuid4(),
        "allocation_percent": 0,
    }
    experiment.update(overrides)
    return experiment


def _service(experiment, executors, *, repo=None):
    repo = repo or _FakeStrategyRepo(experiment)
    service = ShadowExecutorService(
        lambda: _FakeUow(repo), executors=executors, clock=lambda: NOW,
    )
    return service, repo


async def test_shadow_runs_both_executors_and_appends_matching_observation() -> None:
    experiment = _experiment()
    calls = []

    async def control(subject_key, as_of):
        calls.append(("control", subject_key, as_of))
        return {"rank": 2, "score": 0.71}

    async def treatment(subject_key, as_of):
        calls.append(("treatment", subject_key, as_of))
        return {"rank": 2, "score": 0.71}

    service, repo = _service(experiment, {
        experiment["control_strategy_version_id"]: control,
        experiment["treatment_strategy_version_id"]: treatment,
    })
    report = await service.execute(experiment["experiment_id"], "SH:601233")

    # 同一 immutable input 喂给两侧
    assert calls == [
        ("control", "SH:601233", NOW), ("treatment", "SH:601233", NOW),
    ]
    # assignment 被真消费（分桶真的被 Runtime 调用）
    assert repo.assign_calls == [(experiment["experiment_id"], "SH:601233")]
    assert report["assignment"]["assignment"] == "CONTROL"
    # 输出一致 → 非实质分歧
    assert report["materially_divergent"] is False
    assert report["divergence_reason"] is None
    assert report["control"]["output_hash"] == canonical_hash({"rank": 2, "score": 0.71})
    assert report["treatment"]["output_hash"] == canonical_hash({"rank": 2, "score": 0.71})
    assert report["control"]["executed"] and report["treatment"]["executed"]
    assert report["control"]["error"] is None and report["treatment"]["error"] is None
    assert report["latency_ms"] >= 0
    # AI Decision 层边界：不重算、不伪造
    assert report["ai_decision_layer"] == {
        "executed": False,
        "boundary": "SERVER_HAS_NO_MODEL_API",
        "reason": "AI_DECISION_REQUIRES_EXTERNAL_MODEL",
    }
    # ShadowObservation append-only 落库
    assert len(repo.observed) == 1
    observation = repo.observed[0]
    assert observation.experiment_id == experiment["experiment_id"]
    assert observation.subject_key == "SH:601233"
    assert observation.control_payload == {"rank": 2, "score": 0.71}
    assert observation.treatment_payload == {"rank": 2, "score": 0.71}
    assert observation.materially_divergent is False


async def test_divergent_outputs_recorded_with_reason_and_diff() -> None:
    experiment = _experiment()

    async def control(subject_key, as_of):
        return {"rank": 1, "score": 0.80}

    async def treatment(subject_key, as_of):
        return {"rank": 2, "score": 0.80}

    service, repo = _service(experiment, {
        experiment["control_strategy_version_id"]: control,
        experiment["treatment_strategy_version_id"]: treatment,
    })
    report = await service.execute(experiment["experiment_id"], "SH:600519")
    assert report["materially_divergent"] is True
    assert "rank" in report["divergence_reason"]
    assert report["diff"]["paths"] == ["rank"]
    observation = repo.observed[0]
    assert observation.materially_divergent is True
    assert "rank" in observation.divergence_reason
    assert observation.control_output_hash != observation.treatment_output_hash


async def test_unregistered_executor_recorded_as_error_not_fabricated() -> None:
    experiment = _experiment()

    async def control(subject_key, as_of):
        return {"rank": 3}

    service, repo = _service(experiment, {
        experiment["control_strategy_version_id"]: control,
        # treatment 版本没有注册 executor
    })
    report = await service.execute(experiment["experiment_id"], "SH:601318")
    assert report["treatment"]["executed"] is False
    assert report["treatment"]["error"] == "EXECUTOR_NOT_AVAILABLE"
    # hash 仍为 64-hex（对错误事实的 hash，不是伪造的输出）
    assert len(report["treatment"]["output_hash"]) == 64
    assert report["materially_divergent"] is True
    assert "EXECUTOR_NOT_AVAILABLE" in report["divergence_reason"]
    assert "EXECUTOR_NOT_AVAILABLE" in report["error"]
    observation = repo.observed[0]
    assert observation.materially_divergent is True


async def test_shadow_without_control_version_records_control_error() -> None:
    experiment = _experiment(control_strategy_version_id=None)

    async def treatment(subject_key, as_of):
        return {"rank": 1}

    service, repo = _service(experiment, {
        experiment["treatment_strategy_version_id"]: treatment,
    })
    report = await service.execute(experiment["experiment_id"], "SH:601233")
    assert report["control"]["executed"] is False
    assert report["control"]["error"] == "CONTROL_VERSION_UNDEFINED"
    assert report["treatment"]["executed"] is True
    assert report["materially_divergent"] is True
    assert "CONTROL_VERSION_UNDEFINED" in report["divergence_reason"]


async def test_ab_assignment_consumed_and_live_side_reported() -> None:
    experiment = _experiment(
        experiment_type="AB", allocation_percent=25,
    )

    async def control(subject_key, as_of):
        return {"rank": 1}

    async def treatment(subject_key, as_of):
        return {"rank": 1}

    service, repo = _service(experiment, {
        experiment["control_strategy_version_id"]: control,
        experiment["treatment_strategy_version_id"]: treatment,
    })
    report = await service.execute(experiment["experiment_id"], "SH:601233")
    # A/B：assignment 决定 live 侧；ShadowObservation 仍记录两侧事实
    assert report["assignment"]["assignment"] == "CONTROL"
    assert report["live"]["side"] == "CONTROL"
    assert report["live"]["strategy_version_id"] == (
        experiment["control_strategy_version_id"]
    )
    assert report["materially_divergent"] is False
    assert len(repo.observed) == 1
