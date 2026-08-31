from __future__ import annotations

from uuid import UUID

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
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def add_strategy_version(self, command: StrategyVersionCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.strategies.add_strategy_version(command)
            await uow.commit()
        return {"strategy_version_id": object_id, "status": "VERSIONED"}

    async def add_proposal(self, command: StrategyProposalCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.strategies.add_proposal(command)
            await uow.commit()
        return {"proposal_id": object_id, "status": "PROPOSED_NOT_ACTIVE"}

    async def add_guardrail(self, command: GuardrailVersionCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.strategies.add_guardrail(command)
            await uow.commit()
        return {"guardrail_version_id": object_id, "status": "VERSIONED"}

    async def add_experiment(self, command: StrategyExperimentCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.strategies.add_experiment(command)
            await uow.commit()
        return {"experiment_id": object_id, "status": "CREATED"}

    async def experiment_event(
        self, experiment_id: UUID, command: ExperimentEventCommand,
    ):
        async with self._uow_factory() as uow:
            event_id = await uow.strategies.append_experiment_event(
                experiment_id, command
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
    ):
        async with self._uow_factory() as uow:
            result = await uow.strategies.activate(environment, command)
            await uow.commit()
        return result

    async def rollback(
        self, environment: str, command: StrategyRollbackCommand,
    ):
        async with self._uow_factory() as uow:
            result = await uow.strategies.rollback(environment, command)
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
