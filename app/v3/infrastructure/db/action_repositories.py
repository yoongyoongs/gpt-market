from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.domain.action import ActionCandidateCreate, EntryAssessmentCreate, content_hash
from app.v3.infrastructure.db.models import (
    ActionCandidateModel,
    ContextPackModel,
    EntryAssessmentModel,
    RawOpportunityModel,
    PositionReviewModel,
)
from app.v3.repositories.errors import RepositoryConflictError, RepositoryNotFoundError


class SQLAlchemyActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_action(self, command: ActionCandidateCreate) -> UUID:
        raw = await self._session.get(RawOpportunityModel, command.raw_opportunity_id)
        if raw is None:
            raise RepositoryNotFoundError("raw opportunity not found")
        if raw.security_id != command.security_id:
            raise RepositoryConflictError("action candidate security differs from raw opportunity")
        context = await self._session.get(ContextPackModel, command.context_pack_id)
        if context is None or context.content_hash != command.context_pack_hash:
            raise RepositoryConflictError("action candidate context hash is invalid")
        self._session.add(ActionCandidateModel(
            action_candidate_id=command.action_candidate_id,
            raw_opportunity_id=command.raw_opportunity_id,
            security_id=command.security_id, task_run_id=command.task_run_id,
            context_pack_id=command.context_pack_id,
            context_pack_hash=command.context_pack_hash,
            action_state=command.action_state.value,
            expected_horizon=command.expected_horizon.value,
            time_efficiency=command.time_efficiency.value,
            time_efficiency_reason=command.time_efficiency_reason,
            supporting_facts=command.supporting_facts,
            contrary_facts=command.contrary_facts,
            conditions=command.conditions, as_of=command.as_of,
            content_hash=content_hash(command),
        ))
        return command.action_candidate_id

    async def add_entry(self, command: EntryAssessmentCreate) -> UUID:
        if await self._session.get(ActionCandidateModel, command.action_candidate_id) is None:
            raise RepositoryNotFoundError("action candidate not found")
        self._session.add(EntryAssessmentModel(
            entry_assessment_id=command.entry_assessment_id,
            action_candidate_id=command.action_candidate_id,
            entry_plan_id=command.entry_plan_id,
            readiness=command.readiness.value,
            trigger_facts=command.trigger_facts,
            cancel_facts=command.cancel_facts,
            time_efficiency=command.time_efficiency.value,
            explanation=command.explanation, as_of=command.as_of,
            content_hash=content_hash(command),
        ))
        return command.entry_assessment_id

    async def read_pipeline(self, security_id: UUID, limit: int = 50):
        actions = (await self._session.scalars(select(ActionCandidateModel).where(
            ActionCandidateModel.security_id == security_id
        ).order_by(ActionCandidateModel.as_of.desc()).limit(limit))).all()
        action_ids = [item.action_candidate_id for item in actions]
        entries = (await self._session.scalars(select(EntryAssessmentModel).where(
            EntryAssessmentModel.action_candidate_id.in_(action_ids)
        ).order_by(EntryAssessmentModel.as_of.desc()))).all() if action_ids else []
        entry_by_action = {}
        for item in entries:
            entry_by_action.setdefault(item.action_candidate_id, []).append(
                {column.name: getattr(item, column.name) for column in item.__table__.columns}
            )
        return tuple({
            "raw_opportunity_id": item.raw_opportunity_id,
            "action": {column.name: getattr(item, column.name) for column in item.__table__.columns},
            "entries": tuple(entry_by_action.get(item.action_candidate_id, ())),
        } for item in actions)

    async def read_position_reviews(
        self, account_id: UUID, security_id: UUID, limit: int = 50,
    ):
        rows = (await self._session.scalars(select(PositionReviewModel).where(
            PositionReviewModel.account_id == account_id,
            PositionReviewModel.security_id == security_id,
        ).order_by(PositionReviewModel.as_of.desc()).limit(limit))).all()
        return tuple(
            {column.name: getattr(item, column.name) for column in item.__table__.columns}
            for item in rows
        )
