from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.v3.application.audit_helper import AuditRecorder

from app.v3.domain.decision import (
    DecisionCorrectionCommand,
    WatchlistTransitionCommand,
)


class DecisionStateService:
    """RC-08A：Watchlist Transition / Decision Correction 同事务追加 AuditEvent。"""

    def __init__(self, uow_factory, *, clock: Callable[[], datetime] | None = None) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def _recorder(self, uow) -> AuditRecorder:
        return AuditRecorder(uow, clock=self._clock)

    async def transition_watchlist(
        self, security_id: UUID, command: WatchlistTransitionCommand,
        *, request_id: str | None = None,
    ):
        async with self._uow_factory() as uow:
            result = await uow.ai_imports.transition_watchlist(security_id, command)
            await self._recorder(uow).record(
                action="WATCHLIST_TRANSITIONED", object_type="WATCHLIST_ENTRY",
                object_id=str(security_id), actor_id=command.actor_id,
                request_id=request_id, after=command,
                metadata={"target_state": command.target_state.value},
            )
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
        *, request_id: str | None = None,
    ):
        async with self._uow_factory() as uow:
            object_id = await uow.ai_imports.add_decision_correction(
                decision_id, command
            )
            await self._recorder(uow).record(
                action="DECISION_CORRECTION_APPENDED",
                object_type="DECISION_CORRECTION", object_id=str(object_id),
                actor_id=command.corrected_by, request_id=request_id,
                before=command.old_values, after=command.new_values,
                metadata={"decision_id": str(decision_id),
                          "reason": command.reason},
            )
            await uow.commit()
        return {"correction_id": object_id, "status": "APPENDED"}
