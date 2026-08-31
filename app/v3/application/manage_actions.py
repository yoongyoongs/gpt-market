from __future__ import annotations

from uuid import UUID

from app.v3.domain.action import ActionCandidateCreate, EntryAssessmentCreate


class ActionWriteService:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def add_action(self, command: ActionCandidateCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.actions.add_action(command)
            await uow.commit()
        return {"action_candidate_id": object_id, "layer": "ACTION"}

    async def add_entry(self, command: EntryAssessmentCreate):
        async with self._uow_factory() as uow:
            object_id = await uow.actions.add_entry(command)
            await uow.commit()
        return {"entry_assessment_id": object_id, "layer": "ENTRY"}

    async def read_pipeline(self, security_id: UUID, limit: int):
        async with self._uow_factory() as uow:
            return await uow.actions.read_pipeline(security_id, limit)

    async def read_position_reviews(
        self, account_id: UUID, security_id: UUID, limit: int,
    ):
        async with self._uow_factory() as uow:
            return await uow.actions.read_position_reviews(account_id, security_id, limit)
