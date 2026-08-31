from __future__ import annotations

from uuid import UUID

from app.v3.domain.decision import (
    DecisionCorrectionCommand,
    WatchlistTransitionCommand,
)


class DecisionStateService:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def transition_watchlist(
        self, security_id: UUID, command: WatchlistTransitionCommand,
    ):
        async with self._uow_factory() as uow:
            result = await uow.ai_imports.transition_watchlist(security_id, command)
            await uow.commit()
        return result

    async def read_watchlist(self, state: str | None, limit: int):
        async with self._uow_factory() as uow:
            return await uow.ai_imports.read_watchlist(state, limit)

    async def read_decision_state(self, security_id: UUID):
        async with self._uow_factory() as uow:
            return await uow.ai_imports.read_decision_state(security_id)

    async def add_correction(
        self, decision_id: UUID, command: DecisionCorrectionCommand,
    ):
        async with self._uow_factory() as uow:
            object_id = await uow.ai_imports.add_decision_correction(
                decision_id, command
            )
            await uow.commit()
        return {"correction_id": object_id, "status": "APPENDED"}
