from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.domain.hashing import canonical_hash
from app.v3.domain.strategy import (
    CapacityEvaluationCreate,
    ExperimentEventCommand,
    GuardrailVersionCreate,
    OperationalHealthEventCreate,
    ReleaseMode,
    ShadowObservationCreate,
    StrategyActivationCommand,
    StrategyExperimentCreate,
    StrategyProposalCreate,
    StrategyRollbackCommand,
    StrategyVersionCreate,
    content_hash,
)
from app.v3.infrastructure.db.models import (
    CapacityEvaluationModel,
    GuardrailVersionModel,
    OperationalHealthEventModel,
    RegressionCaseModel,
    ReleaseEventModel,
    ReleaseStateModel,
    ReplayRunModel,
    ShadowObservationModel,
    StrategyExperimentEventModel,
    StrategyExperimentModel,
    StrategyProposalModel,
    StrategyVersionModel,
)
from app.v3.repositories.errors import RepositoryConflictError, RepositoryNotFoundError


class SQLAlchemyStrategyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_strategy_version(self, command: StrategyVersionCreate) -> UUID:
        existing = await self._session.scalar(select(StrategyVersionModel).where(
            StrategyVersionModel.strategy_code == command.strategy_code,
            StrategyVersionModel.version == command.version,
        ))
        if existing is not None:
            # 幂等重放：同 code+version 同内容 → 返回原 id；内容不同 → 明确冲突
            same = (
                existing.configuration == command.configuration
                and existing.rationale == command.rationale
                and existing.created_by == command.created_by
                and existing.supersedes_strategy_version_id
                == command.supersedes_strategy_version_id
            )
            if not same:
                raise RepositoryConflictError(
                    "strategy version identity exists with different content"
                )
            return existing.strategy_version_id
        if command.version > 1:
            previous = await self._session.get(
                StrategyVersionModel, command.supersedes_strategy_version_id
            )
            if previous is None or previous.strategy_code != command.strategy_code or previous.version != command.version - 1:
                raise RepositoryConflictError("strategy version chain is invalid")
        self._session.add(StrategyVersionModel(
            **command.model_dump(mode="python"), content_hash=content_hash(command)
        ))
        return command.strategy_version_id

    async def add_proposal(self, command: StrategyProposalCreate) -> UUID:
        if await self._session.get(StrategyVersionModel, command.proposed_strategy_version_id) is None:
            raise RepositoryNotFoundError("proposed strategy version not found")
        if command.actor_type.value == "AI" and command.source_result_id is None:
            raise RepositoryConflictError("AI strategy proposal requires a source result")
        values = command.model_dump(mode="python")
        values["actor_type"] = command.actor_type.value
        values["risks"] = list(command.risks)
        self._session.add(StrategyProposalModel(
            **values, content_hash=content_hash(command)
        ))
        return command.proposal_id

    async def add_guardrail(self, command: GuardrailVersionCreate) -> UUID:
        if command.version > 1:
            previous = await self._session.get(
                GuardrailVersionModel, command.supersedes_guardrail_version_id
            )
            if previous is None or previous.guardrail_code != command.guardrail_code or previous.version != command.version - 1:
                raise RepositoryConflictError("guardrail version chain is invalid")
        self._session.add(GuardrailVersionModel(
            **command.model_dump(mode="python"), content_hash=content_hash(command)
        ))
        return command.guardrail_version_id

    async def add_experiment(self, command: StrategyExperimentCreate) -> UUID:
        treatment = await self._session.get(
            StrategyVersionModel, command.treatment_strategy_version_id
        )
        guardrail = await self._session.get(
            GuardrailVersionModel, command.guardrail_version_id
        )
        if treatment is None or guardrail is None:
            raise RepositoryNotFoundError("strategy or guardrail version not found")
        if command.control_strategy_version_id is not None and await self._session.get(
            StrategyVersionModel, command.control_strategy_version_id
        ) is None:
            raise RepositoryNotFoundError("control strategy version not found")
        values = command.model_dump(mode="python")
        values["experiment_type"] = command.experiment_type.value
        self._session.add(StrategyExperimentModel(
            **values, content_hash=content_hash(command)
        ))
        await self._session.flush()
        await self._append_experiment_event(
            command.experiment_id, "CREATED", "HUMAN", command.created_by,
            "experiment configuration created",
        )
        return command.experiment_id

    async def append_experiment_event(
        self, experiment_id: UUID, command: ExperimentEventCommand,
    ) -> UUID:
        if await self._session.get(StrategyExperimentModel, experiment_id, with_for_update=True) is None:
            raise RepositoryNotFoundError("strategy experiment not found")
        latest = await self._session.scalar(select(StrategyExperimentEventModel).where(
            StrategyExperimentEventModel.experiment_id == experiment_id
        ).order_by(StrategyExperimentEventModel.sequence.desc()).limit(1))
        if latest is not None and (
            latest.event_type == command.event_type
            and latest.actor_id == command.actor_id
            and latest.reason == command.reason
        ):
            # 幂等重放：与最新事件完全一致的重复请求 → 返回原事件 id
            return latest.event_id
        current = latest.event_type if latest else "CREATED"
        allowed = {
            "CREATED": {"STARTED", "STOPPED"},
            "STARTED": {"PAUSED", "COMPLETED", "STOPPED"},
            "PAUSED": {"RESUMED", "STOPPED"},
            "RESUMED": {"PAUSED", "COMPLETED", "STOPPED"},
            "COMPLETED": set(), "STOPPED": set(),
        }
        if command.event_type not in allowed.get(current, set()):
            raise RepositoryConflictError(
                f"invalid experiment transition: {current} -> {command.event_type}"
            )
        return await self._append_experiment_event(
            experiment_id, command.event_type, command.actor_type.value,
            command.actor_id, command.reason,
        )

    async def _append_experiment_event(
        self, experiment_id: UUID, event_type: str, actor_type: str,
        actor_id: str, reason: str,
    ) -> UUID:
        sequence = (await self._session.scalar(select(func.max(
            StrategyExperimentEventModel.sequence
        )).where(StrategyExperimentEventModel.experiment_id == experiment_id)) or 0) + 1
        event_id = uuid4()
        now = datetime.now(timezone.utc)
        payload = {"experiment_id": experiment_id, "sequence": sequence,
                   "event_type": event_type, "actor_type": actor_type,
                   "actor_id": actor_id, "reason": reason, "event_time": now}
        self._session.add(StrategyExperimentEventModel(
            event_id=event_id, content_hash=canonical_hash(payload), **payload
        ))
        return event_id

    async def add_shadow_observation(self, command: ShadowObservationCreate) -> UUID:
        experiment = await self._session.get(StrategyExperimentModel, command.experiment_id)
        if experiment is None:
            raise RepositoryNotFoundError("strategy experiment not found")
        latest = await self._session.scalar(select(StrategyExperimentEventModel).where(
            StrategyExperimentEventModel.experiment_id == command.experiment_id
        ).order_by(StrategyExperimentEventModel.sequence.desc()).limit(1))
        if latest is None or latest.event_type not in {"STARTED", "RESUMED"}:
            raise RepositoryConflictError("experiment is not running")
        if command.materially_divergent and not command.divergence_reason:
            raise RepositoryConflictError("material divergence requires a reason")
        self._session.add(ShadowObservationModel(
            **command.model_dump(mode="python"), content_hash=content_hash(command)
        ))
        return command.shadow_observation_id

    async def active_experiments(self, *, as_of: datetime) -> list[dict]:
        """STR-001 Shadow Runtime：当前应被自动观察的实验。

        活跃 = 时间窗内（starts_at <= as_of 且 ends_at 未到）且最新事件为
        STARTED/RESUMED（与 add_shadow_observation/assign_experiment 的
        运行校验一致），避免 Scheduler 对已停止实验盲目打冲突。
        """
        latest_event = (
            select(
                StrategyExperimentEventModel.experiment_id.label("experiment_id"),
                func.max(StrategyExperimentEventModel.sequence).label("sequence"),
            )
            .group_by(StrategyExperimentEventModel.experiment_id)
            .subquery()
        )
        rows = (
            await self._session.scalars(
                select(StrategyExperimentModel)
                .join(
                    latest_event,
                    latest_event.c.experiment_id
                    == StrategyExperimentModel.experiment_id,
                )
                .join(
                    StrategyExperimentEventModel,
                    and_(
                        StrategyExperimentEventModel.experiment_id
                        == latest_event.c.experiment_id,
                        StrategyExperimentEventModel.sequence
                        == latest_event.c.sequence,
                    ),
                )
                .where(
                    StrategyExperimentEventModel.event_type.in_(
                        ("STARTED", "RESUMED")
                    ),
                    StrategyExperimentModel.starts_at <= as_of,
                    or_(
                        StrategyExperimentModel.ends_at.is_(None),
                        StrategyExperimentModel.ends_at > as_of,
                    ),
                )
                .order_by(StrategyExperimentModel.starts_at)
            )
        ).all()
        return [
            {
                "experiment_id": row.experiment_id,
                "experiment_type": row.experiment_type,
                "control_strategy_version_id": row.control_strategy_version_id,
                "treatment_strategy_version_id": row.treatment_strategy_version_id,
                "allocation_percent": row.allocation_percent,
                "starts_at": row.starts_at,
                "ends_at": row.ends_at,
            }
            for row in rows
        ]

    async def shadow_observation_exists(
        self, experiment_id: UUID, subject_key: str, *,
        observed_from: datetime, observed_to: datetime,
    ) -> bool:
        """STR-001 Scheduler 幂等：observed_at 窗口内同 experiment×subject 已观察。"""
        return (
            await self._session.scalar(
                select(ShadowObservationModel.shadow_observation_id).where(
                    ShadowObservationModel.experiment_id == experiment_id,
                    ShadowObservationModel.subject_key == subject_key,
                    ShadowObservationModel.observed_at > observed_from,
                    ShadowObservationModel.observed_at <= observed_to,
                ).limit(1)
            )
        ) is not None

    async def assign_experiment(self, experiment_id: UUID, subject_key: str) -> dict:
        experiment = await self._session.get(StrategyExperimentModel, experiment_id)
        if experiment is None:
            raise RepositoryNotFoundError("strategy experiment not found")
        latest = await self._session.scalar(select(StrategyExperimentEventModel).where(
            StrategyExperimentEventModel.experiment_id == experiment_id
        ).order_by(StrategyExperimentEventModel.sequence.desc()).limit(1))
        if latest is None or latest.event_type not in {"STARTED", "RESUMED"}:
            raise RepositoryConflictError("experiment is not running")
        bucket = int(hashlib.sha256(
            f"{experiment_id}:{subject_key}".encode("utf-8")
        ).hexdigest()[:8], 16) % 100
        treatment = experiment.experiment_type == "AB" and bucket < experiment.allocation_percent
        return {
            "experiment_id": experiment_id, "subject_key": subject_key,
            "bucket": bucket,
            "assignment": "TREATMENT" if treatment else "CONTROL",
            "strategy_version_id": (
                experiment.treatment_strategy_version_id
                if treatment or experiment.experiment_type == "SHADOW"
                else experiment.control_strategy_version_id
            ),
            "shadow_only": experiment.experiment_type == "SHADOW",
        }

    async def evaluate_capacity(self, command: CapacityEvaluationCreate) -> dict:
        guardrail = await self._session.get(
            GuardrailVersionModel, command.guardrail_version_id
        )
        if guardrail is None:
            raise RepositoryNotFoundError("guardrail version not found")
        if await self._session.get(StrategyVersionModel, command.strategy_version_id) is None:
            raise RepositoryNotFoundError("strategy version not found")
        experiment_ids = (await self._session.scalars(select(
            StrategyExperimentModel.experiment_id
        ).where(
            StrategyExperimentModel.treatment_strategy_version_id == command.strategy_version_id,
            StrategyExperimentModel.guardrail_version_id == command.guardrail_version_id,
        ))).all()
        observations = (await self._session.scalars(select(ShadowObservationModel).where(
            ShadowObservationModel.experiment_id.in_(experiment_ids),
            ShadowObservationModel.observed_at <= command.evaluated_at,
        ))).all() if experiment_ids else []
        if not observations:
            raise RepositoryConflictError("capacity evaluation requires recorded shadow observations")
        sample_count = len(observations)
        error_rate = sum(item.error is not None for item in observations) / sample_count
        divergence_rate = sum(item.materially_divergent for item in observations) / sample_count
        latencies = sorted(float(item.latency_ms) for item in observations)
        p95_index = max(0, min(len(latencies) - 1, int(0.95 * len(latencies) + 0.999999) - 1))
        p95_ms = latencies[p95_index]
        failures = []
        if sample_count < guardrail.min_shadow_sample_count:
            failures.append("INSUFFICIENT_SHADOW_SAMPLES")
        if error_rate > float(guardrail.max_error_rate):
            failures.append("ERROR_RATE_EXCEEDED")
        if p95_ms > float(guardrail.max_p95_ms):
            failures.append("P95_LATENCY_EXCEEDED")
        if divergence_rate > float(guardrail.max_divergence_rate):
            failures.append("DIVERGENCE_RATE_EXCEEDED")
        if command.capacity_utilization > float(guardrail.max_capacity_utilization):
            failures.append("CAPACITY_UTILIZATION_EXCEEDED")
        if guardrail.rollback_on_provider_failure and command.provider_failures:
            failures.append("PROVIDER_FAILURE_PRESENT")
        values = command.model_dump(mode="python")
        values.update(sample_count=sample_count, error_rate=error_rate,
                      p95_ms=p95_ms, divergence_rate=divergence_rate)
        values.update(passed=not failures, failures=failures)
        payload = {**command.model_dump(mode="json"), "sample_count": sample_count,
                   "error_rate": error_rate, "p95_ms": p95_ms,
                   "divergence_rate": divergence_rate, "passed": not failures,
                   "failures": failures}
        self._session.add(CapacityEvaluationModel(
            **values, content_hash=canonical_hash(payload)
        ))
        return {"capacity_evaluation_id": command.capacity_evaluation_id,
                "passed": not failures, "failures": tuple(failures),
                "measured": {"sample_count": sample_count,
                             "error_rate": error_rate, "p95_ms": p95_ms,
                             "divergence_rate": divergence_rate}}

    async def activate(
        self, environment: str, command: StrategyActivationCommand,
    ) -> dict:
        self._validate_environment(environment)
        strategy = await self._session.get(
            StrategyVersionModel, command.strategy_version_id
        )
        guardrail = await self._session.get(
            GuardrailVersionModel, command.guardrail_version_id
        )
        if strategy is None or guardrail is None:
            raise RepositoryNotFoundError("strategy or guardrail version not found")
        proposal = await self._session.get(StrategyProposalModel, command.proposal_id)
        if proposal is None:
            raise RepositoryNotFoundError("approved strategy proposal not found")
        if proposal.proposed_strategy_version_id != command.strategy_version_id:
            raise RepositoryConflictError("proposal does not reference the activated strategy")
        state = await self._release_state(environment, lock=True)
        if state.row_version != command.expected_row_version:
            raise RepositoryConflictError("release state changed; refresh before activation")
        gates = await self._activation_gates(strategy, guardrail, command.target_mode)
        if not gates["passed"]:
            raise RepositoryConflictError(
                "activation gates failed: " + ", ".join(gates["failures"])
            )
        from_mode = state.mode
        state.mode = command.target_mode.value
        state.active_strategy_version_id = command.strategy_version_id
        state.active_guardrail_version_id = command.guardrail_version_id
        state.row_version += 1
        state.updated_at = datetime.now(timezone.utc)
        event_id = await self._append_release_event(
            environment, from_mode, state.mode, command.proposal_id,
            command.strategy_version_id,
            command.guardrail_version_id, command.actor_type.value,
            command.actor_id, command.approval_reason, gates,
        )
        return {"release_event_id": event_id, "environment": environment,
                "mode": state.mode, "row_version": state.row_version,
                "gate_snapshot": gates}

    async def rollback(
        self, environment: str, command: StrategyRollbackCommand,
    ) -> dict:
        self._validate_environment(environment)
        state = await self._release_state(environment, lock=True)
        if state.row_version != command.expected_row_version:
            raise RepositoryConflictError("release state changed; refresh before rollback")
        from_mode = state.mode
        state.mode = ReleaseMode.V2.value
        state.active_strategy_version_id = None
        state.active_guardrail_version_id = None
        state.row_version += 1
        state.updated_at = datetime.now(timezone.utc)
        snapshot = {"passed": True, "failures": [], "rollback": True}
        event_id = await self._append_release_event(
            environment, from_mode, state.mode, None, None, None,
            command.actor_type.value, command.actor_id, command.reason, snapshot,
        )
        return {"release_event_id": event_id, "environment": environment,
                "mode": state.mode, "row_version": state.row_version}

    async def _activation_gates(
        self, strategy: StrategyVersionModel, guardrail: GuardrailVersionModel,
        target_mode: ReleaseMode,
    ) -> dict:
        if target_mode is ReleaseMode.SHADOW:
            return {"passed": True, "failures": [], "target_mode": target_mode.value}
        failures = []
        capacity = await self._session.scalar(select(CapacityEvaluationModel).where(
            CapacityEvaluationModel.strategy_version_id == strategy.strategy_version_id,
            CapacityEvaluationModel.guardrail_version_id == guardrail.guardrail_version_id,
        ).order_by(CapacityEvaluationModel.evaluated_at.desc()).limit(1))
        if capacity is None or not capacity.passed:
            failures.append("CAPACITY_GATE_NOT_PASSED")
        experiments = (await self._session.scalars(select(StrategyExperimentModel).where(
            StrategyExperimentModel.treatment_strategy_version_id == strategy.strategy_version_id,
            StrategyExperimentModel.guardrail_version_id == guardrail.guardrail_version_id,
        ))).all()
        experiment_ids = [item.experiment_id for item in experiments]
        latest_events = (await self._session.scalars(select(StrategyExperimentEventModel).where(
            StrategyExperimentEventModel.experiment_id.in_(experiment_ids)
        ).order_by(StrategyExperimentEventModel.sequence.desc()))).all() if experiment_ids else []
        latest_by_experiment = {}
        for event in latest_events:
            latest_by_experiment.setdefault(event.experiment_id, event)
        if target_mode is ReleaseMode.AB and not any(
            item.experiment_type == "AB"
            and latest_by_experiment.get(item.experiment_id) is not None
            and latest_by_experiment[item.experiment_id].event_type in {"STARTED", "RESUMED"}
            for item in experiments
        ):
            failures.append("RUNNING_AB_EXPERIMENT_REQUIRED")
        strategy_keys = (strategy.strategy_code, f"{strategy.strategy_code}:v{strategy.version}")
        if target_mode is ReleaseMode.V3:
            if not any(
                latest_by_experiment.get(item.experiment_id) is not None
                and latest_by_experiment[item.experiment_id].event_type == "COMPLETED"
                for item in experiments
            ):
                failures.append("COMPLETED_SHADOW_OR_AB_REQUIRED")
            replay = await self._session.scalar(select(ReplayRunModel).where(
                ReplayRunModel.strategy_version.in_(strategy_keys),
                ReplayRunModel.status == "COMPLETED",
            ).order_by(ReplayRunModel.created_at.desc()).limit(1))
            if replay is None:
                failures.append("POINT_IN_TIME_REPLAY_NOT_COMPLETED")
            blocked_regression = await self._session.scalar(select(func.count()).select_from(
                RegressionCaseModel
            ).where(
                RegressionCaseModel.strategy_version.in_(strategy_keys),
                RegressionCaseModel.status == "BLOCKED",
            )) or 0
            if blocked_regression:
                failures.append("BLOCKED_REGRESSION_CASES_PRESENT")
            active_regression = await self._session.scalar(select(func.count()).select_from(
                RegressionCaseModel
            ).where(
                RegressionCaseModel.strategy_version.in_(strategy_keys),
                RegressionCaseModel.status == "ACTIVE",
            )) or 0
            if not active_regression:
                failures.append("REGRESSION_CASES_NOT_RECORDED")
        return {
            "passed": not failures, "failures": failures,
            "target_mode": target_mode.value,
            "capacity_evaluation_id": str(capacity.capacity_evaluation_id) if capacity else None,
        }

    async def _release_state(self, environment: str, *, lock: bool):
        self._validate_environment(environment)
        statement = select(ReleaseStateModel).where(
            ReleaseStateModel.environment == environment
        )
        if lock:
            statement = statement.with_for_update()
        state = await self._session.scalar(statement)
        if state is None:
            state = ReleaseStateModel(
                release_state_id=uuid4(), environment=environment,
                mode="V2", row_version=0, updated_at=datetime.now(timezone.utc),
            )
            self._session.add(state)
            await self._session.flush()
        return state

    @staticmethod
    def _validate_environment(environment: str) -> None:
        if not environment or len(environment) > 32:
            raise RepositoryConflictError(
                "release environment must contain between 1 and 32 characters"
            )

    async def _append_release_event(
        self, environment: str, from_mode: str, to_mode: str,
        proposal_id: UUID | None, strategy_version_id: UUID | None,
        guardrail_version_id: UUID | None,
        actor_type: str, actor_id: str, reason: str, gate_snapshot: dict,
    ) -> UUID:
        sequence = (await self._session.scalar(select(func.max(
            ReleaseEventModel.sequence
        )).where(ReleaseEventModel.environment == environment)) or 0) + 1
        event_id = uuid4()
        now = datetime.now(timezone.utc)
        payload = {"environment": environment, "sequence": sequence,
                   "from_mode": from_mode, "to_mode": to_mode,
                   "proposal_id": proposal_id,
                   "strategy_version_id": strategy_version_id,
                   "guardrail_version_id": guardrail_version_id,
                   "actor_type": actor_type, "actor_id": actor_id,
                   "reason": reason, "gate_snapshot": gate_snapshot,
                   "event_time": now}
        self._session.add(ReleaseEventModel(
            release_event_id=event_id, content_hash=canonical_hash(payload), **payload
        ))
        return event_id

    async def add_health_event(self, command: OperationalHealthEventCreate) -> dict:
        payload = command.model_dump(mode="json")
        payload.pop("health_event_id")
        identity_hash = canonical_hash(payload)
        existing = await self._session.scalar(
            select(OperationalHealthEventModel).where(
                OperationalHealthEventModel.content_hash == identity_hash
            )
        )
        if existing is not None:
            # 幂等重放：同一健康事件重复上报 → 返回原 id
            return {"health_event_id": existing.health_event_id,
                    "automatic_rollback_event_id": None}
        self._session.add(OperationalHealthEventModel(
            health_event_id=command.health_event_id, environment=command.environment,
            component=command.component,
            capability=command.capability, status=command.status,
            latency_ms=command.latency_ms, error_type=command.error_type,
            circuit_state=command.circuit_state, observed_at=command.observed_at,
            metadata_payload=command.metadata, content_hash=identity_hash,
        ))
        rollback_event_id = None
        state = await self._release_state(command.environment, lock=True)
        if command.status == "FAILED" and state.mode != "V2" and state.active_guardrail_version_id:
            guardrail = await self._session.get(
                GuardrailVersionModel, state.active_guardrail_version_id
            )
            if guardrail is not None and guardrail.rollback_on_provider_failure:
                from_mode = state.mode
                state.mode = "V2"
                state.active_strategy_version_id = None
                state.active_guardrail_version_id = None
                state.row_version += 1
                state.updated_at = datetime.now(timezone.utc)
                rollback_event_id = await self._append_release_event(
                    command.environment, from_mode, "V2", None, None, None,
                    "SYSTEM", "guardrail-monitor",
                    f"automatic rollback after {command.component}/{command.capability} failure",
                    {"passed": False, "failures": ["OPERATIONAL_HEALTH_FAILED"],
                     "health_event_id": str(command.health_event_id)},
                )
        return {"health_event_id": command.health_event_id,
                "automatic_rollback_event_id": rollback_event_id}

    async def read_health_events(self, component: str, limit: int = 20) -> tuple:
        """R5-P1-007/§65：按 component 读最近心跳（observed_at desc），
        API/Dashboard 进程据此跨进程读取 Worker heartbeat。"""
        return tuple((await self._session.scalars(
            select(OperationalHealthEventModel)
            .where(OperationalHealthEventModel.component == component)
            .order_by(
                OperationalHealthEventModel.observed_at.desc(),
                OperationalHealthEventModel.health_event_id.desc(),
            )
            .limit(limit)
        )).all())

    async def resolve_release(self, environment: str) -> dict:
        """RC-07B：唯一 Runtime 解析点——每次调用都读最新 ReleaseState
        （回滚立即生效），返回当前 executor 配置；不完整状态显式回落 V2。"""
        self._validate_environment(environment)
        state = await self._session.scalar(select(ReleaseStateModel).where(
            ReleaseStateModel.environment == environment
        ))
        if state is None:
            return {"mode": "V2", "effective_mode": "V2", "reason": "NO_V3_RELEASE",
                    "strategy_version_id": None, "guardrail_version_id": None,
                    "configuration": None, "row_version": None}
        if state.mode == ReleaseMode.V2.value or state.active_strategy_version_id is None:
            reason = (
                "RELEASE_MODE_V2" if state.mode == ReleaseMode.V2.value
                else "RELEASE_STATE_INCOMPLETE"
            )
            return {"mode": state.mode, "effective_mode": "V2", "reason": reason,
                    "strategy_version_id": None, "guardrail_version_id": None,
                    "configuration": None, "row_version": state.row_version}
        strategy = await self._session.get(
            StrategyVersionModel, state.active_strategy_version_id
        )
        if strategy is None:
            return {"mode": state.mode, "effective_mode": "V2",
                    "reason": "ACTIVE_STRATEGY_VERSION_MISSING",
                    "strategy_version_id": None, "guardrail_version_id": None,
                    "configuration": None, "row_version": state.row_version}
        return {
            "mode": state.mode, "effective_mode": state.mode, "reason": None,
            "strategy_version_id": strategy.strategy_version_id,
            "guardrail_version_id": state.active_guardrail_version_id,
            "configuration": dict(strategy.configuration or {}),
            "row_version": state.row_version,
        }

    async def release_dashboard(self, environment: str) -> dict:
        self._validate_environment(environment)
        state = await self._session.scalar(select(ReleaseStateModel).where(
            ReleaseStateModel.environment == environment
        ))
        events = (await self._session.scalars(select(ReleaseEventModel).where(
            ReleaseEventModel.environment == environment
        ).order_by(ReleaseEventModel.sequence.desc()).limit(20))).all()
        health = (await self._session.scalars(select(OperationalHealthEventModel).where(
            OperationalHealthEventModel.environment == environment
        ).order_by(
            OperationalHealthEventModel.observed_at.desc()
        ).limit(100))).all()
        def serialize(row):
            return {
                column.name: getattr(row, column.name)
                for column in row.__table__.columns
            }
        state_payload = serialize(state) if state is not None else {
            "release_state_id": None, "environment": environment,
            "mode": "V2", "active_strategy_version_id": None,
            "active_guardrail_version_id": None, "row_version": 0,
            "updated_at": None,
        }
        return {"state": state_payload,
                "recent_release_events": tuple(serialize(item) for item in events),
                "recent_health_events": tuple(serialize(item) for item in health),
                "actual_runtime_note": "database release state does not change the process feature flag by itself"}

    async def strategy_catalog(self, limit: int) -> dict:
        strategies = (await self._session.scalars(select(StrategyVersionModel).order_by(
            StrategyVersionModel.strategy_code, StrategyVersionModel.version.desc()
        ).limit(limit))).all()
        proposals = (await self._session.scalars(select(StrategyProposalModel).order_by(
            StrategyProposalModel.created_at.desc()
        ).limit(limit))).all()
        guardrails = (await self._session.scalars(select(GuardrailVersionModel).order_by(
            GuardrailVersionModel.guardrail_code, GuardrailVersionModel.version.desc()
        ).limit(limit))).all()
        def serialize(row):
            return {
                column.name: getattr(row, column.name)
                for column in row.__table__.columns
            }
        return {"strategy_versions": tuple(serialize(item) for item in strategies),
                "proposals": tuple(serialize(item) for item in proposals),
                "guardrail_versions": tuple(serialize(item) for item in guardrails)}

    async def experiment_detail(self, experiment_id: UUID) -> dict:
        experiment = await self._session.get(StrategyExperimentModel, experiment_id)
        if experiment is None:
            raise RepositoryNotFoundError("strategy experiment not found")
        events = (await self._session.scalars(select(StrategyExperimentEventModel).where(
            StrategyExperimentEventModel.experiment_id == experiment_id
        ).order_by(StrategyExperimentEventModel.sequence))).all()
        observations = (await self._session.scalars(select(ShadowObservationModel).where(
            ShadowObservationModel.experiment_id == experiment_id
        ).order_by(ShadowObservationModel.observed_at.desc()).limit(200))).all()
        evaluations = (await self._session.scalars(select(CapacityEvaluationModel).where(
            CapacityEvaluationModel.strategy_version_id == experiment.treatment_strategy_version_id,
            CapacityEvaluationModel.guardrail_version_id == experiment.guardrail_version_id,
        ).order_by(CapacityEvaluationModel.evaluated_at.desc()).limit(20))).all()
        def serialize(row):
            return {
                column.name: getattr(row, column.name)
                for column in row.__table__.columns
            }
        return {"experiment": serialize(experiment),
                "current_status": events[-1].event_type if events else "UNKNOWN",
                "events": tuple(serialize(item) for item in events),
                "shadow_observations": tuple(serialize(item) for item in observations),
                "capacity_evaluations": tuple(serialize(item) for item in evaluations)}
