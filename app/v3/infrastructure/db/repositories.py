from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.contracts.agent import AgentTask
from app.v3.domain.audit import AuditEvent
from app.v3.infrastructure.db.models import AgentTaskModel, AuditEventModel


class SQLAlchemyAgentTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, task: AgentTask) -> bool:
        serialized_task = task.model_dump(mode="json")
        statement = (
            insert(AgentTaskModel)
            .values(
                task_id=task.task_id,
                task_run_id=task.task_run_id,
                task_type=task.task_type,
                subject=serialized_task["subject"],
                task_profile=task.task_profile,
                trigger_type=task.trigger_type,
                as_of=task.as_of,
                context_pack_id=task.context_pack_id,
                context_pack_hash=task.context_pack_hash,
                expected_result_type=task.expected_result_type,
                constraints=serialized_task["constraints"],
                content_hash=task.computed_content_hash(),
            )
            .on_conflict_do_nothing(index_elements=[AgentTaskModel.content_hash])
            .returning(AgentTaskModel.task_id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None


class SQLAlchemyAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: AuditEvent) -> None:
        self._session.add(
            AuditEventModel(
                audit_id=event.audit_id,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                action=event.action,
                object_type=event.object_type,
                object_id=event.object_id,
                request_id=event.request_id,
                before_hash=event.before_hash,
                after_hash=event.after_hash,
                result=event.result,
                event_time=event.event_time,
                metadata_payload=event.metadata,
            )
        )
