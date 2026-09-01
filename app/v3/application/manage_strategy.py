from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.v3.application.audit_helper import AuditRecorder

from app.v3.domain.strategy import (
    CapacityEvaluationCreate,
    ExperimentEventCommand,
    GuardrailVersionCreate,
    OperationalHealthEventCreate,
    ShadowObservationCreate,
    StrategyActivationCommand,
    StrategyExperimentCreate,
    StrategyProposalCreate,
    StrategyRollbackCommand,
    StrategyVersionCreate,
)


class StrategyStabilizationService:
    """RC-08A：策略域关键 WRITE 同事务追加 AuditEvent。"""

    def __init__(self, uow_factory, *, clock: Callable[[], datetime] | None = None) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def _recorder(self, uow) -> AuditRecorder:
        return AuditRecorder(uow, clock=self._clock)

    @staticmethod
    def _actor_id(command) -> str | None:
        for field in ("actor_id", "created_by", "confirmed_by"):
            value = getattr(command, field, None)
            if value:
                return str(value)
        return None

    async def add_strategy_version(self, command: StrategyVersionCreate,
                                   *, request_id: str | None = None):
        async with self._uow_factory() as uow:
            object_id = await uow.strategies.add_strategy_version(command)
            await self._recorder(uow).record(
                action="STRATEGY_VERSION_APPENDED", object_type="STRATEGY_VERSION",
                object_id=str(object_id), actor_id=self._actor_id(command),
                request_id=request_id, after=command,
                metadata={"strategy_code": command.strategy_code,
                          "version": command.version},
            )
            await uow.commit()
        return {"strategy_version_id": object_id, "status": "VERSIONED"}

    async def add_proposal(self, command: StrategyProposalCreate,
                           *, request_id: str | None = None):
        async with self._uow_factory() as uow:
            object_id = await uow.strategies.add_proposal(command)
            await self._recorder(uow).record(
                action="STRATEGY_PROPOSED", object_type="STRATEGY_PROPOSAL",
                object_id=str(object_id),
                actor_id=command.actor_id, request_id=request_id, after=command,
                metadata={"proposed_strategy_version_id":
                          str(command.proposed_strategy_version_id)},
            )
            await uow.commit()
        return {"proposal_id": object_id, "status": "PROPOSED_NOT_ACTIVE"}

    async def add_guardrail(self, command: GuardrailVersionCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.strategies.add_guardrail(command)
            await uow.commit()
        return {"guardrail_version_id": object_id, "status": "VERSIONED"}

    async def add_experiment(self, command: StrategyExperimentCreate,
                             *, request_id: str | None = None):
        async with self._uow_factory() as uow:
            object_id = await uow.strategies.add_experiment(command)
            await self._recorder(uow).record(
                action="STRATEGY_EXPERIMENT_CREATED",
                object_type="STRATEGY_EXPERIMENT", object_id=str(object_id),
                actor_id=self._actor_id(command), request_id=request_id,
                after=command,
                metadata={"experiment_type": command.experiment_type.value,
                          "allocation_percent": command.allocation_percent},
            )
            await uow.commit()
        return {"experiment_id": object_id, "status": "CREATED"}

    async def experiment_event(
        self, experiment_id: UUID, command: ExperimentEventCommand,
        *, request_id: str | None = None,
    ):
        async with self._uow_factory() as uow:
            event_id = await uow.strategies.append_experiment_event(
                experiment_id, command
            )
            await self._recorder(uow).record(
                action="STRATEGY_EXPERIMENT_EVENT",
                object_type="STRATEGY_EXPERIMENT_EVENT",
                object_id=str(event_id), actor_id=command.actor_id,
                request_id=request_id, after=command,
                metadata={"experiment_id": str(experiment_id),
                          "event_type": command.event_type},
            )
            await uow.commit()
        return {"event_id": event_id, "status": command.event_type}

    async def shadow_observation(self, command: ShadowObservationCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.strategies.add_shadow_observation(command)
            await uow.commit()
        return {"shadow_observation_id": object_id, "status": "RECORDED"}

    async def assign(self, experiment_id: UUID, subject_key: str):
        async with self._uow_factory() as uow:
            return await uow.strategies.assign_experiment(experiment_id, subject_key)

    async def evaluate_capacity(self, command: CapacityEvaluationCreate):
        async with self._uow_factory() as uow:
            result = await uow.strategies.evaluate_capacity(command)
            await uow.commit()
        return result

    async def activate(
        self, environment: str, command: StrategyActivationCommand,
        *, request_id: str | None = None,
    ):
        async with self._uow_factory() as uow:
            result = await uow.strategies.activate(environment, command)
            await self._recorder(uow).record(
                action="STRATEGY_ACTIVATED", object_type="STRATEGY_RELEASE",
                object_id=str(result.get("release_event_id")),
                actor_id=command.actor_id, request_id=request_id,
                after=result,
                metadata={"environment": environment,
                          "target_mode": command.target_mode.value,
                          "row_version": result.get("row_version")},
            )
            await uow.commit()
        return result

    async def rollback(
        self, environment: str, command: StrategyRollbackCommand,
        *, request_id: str | None = None,
    ):
        async with self._uow_factory() as uow:
            result = await uow.strategies.rollback(environment, command)
            await self._recorder(uow).record(
                action="STRATEGY_ROLLEDBACK", object_type="STRATEGY_RELEASE",
                object_id=str(result.get("release_event_id")),
                actor_id=command.actor_id, request_id=request_id,
                after=result,
                metadata={"environment": environment,
                          "reason": command.reason,
                          "row_version": result.get("row_version")},
            )
            await uow.commit()
        return result

    async def add_health_event(self, command: OperationalHealthEventCreate):
        async with self._uow_factory() as uow:
            result = await uow.strategies.add_health_event(command)
            await uow.commit()
        return {**result, "status": "RECORDED"}

    async def dashboard(self, environment: str):
        async with self._uow_factory() as uow:
            return await uow.strategies.release_dashboard(environment)

    async def catalog(self, limit: int):
        async with self._uow_factory() as uow:
            return await uow.strategies.strategy_catalog(limit)

    async def experiment_detail(self, experiment_id: UUID):
        async with self._uow_factory() as uow:
            return await uow.strategies.experiment_detail(experiment_id)
