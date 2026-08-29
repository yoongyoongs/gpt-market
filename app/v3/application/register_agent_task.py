from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.v3.contracts.agent import AgentTask
from app.v3.domain.audit import AuditEvent
from app.v3.repositories.protocols import UnitOfWork


@dataclass(frozen=True)
class RegisterAgentTaskResult:
    task_id: str
    content_hash: str
    created: bool


class RegisterAgentTaskService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(
        self,
        task: AgentTask,
        *,
        actor_type: str,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> RegisterAgentTaskResult:
        content_hash = task.computed_content_hash()
        async with self._uow_factory() as uow:
            created = await uow.tasks.add_if_absent(task)
            if not created:
                return RegisterAgentTaskResult(str(task.task_id), content_hash, created=False)

            await uow.audits.add(
                AuditEvent(
                    audit_id=uuid4(),
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="AGENT_TASK_REGISTERED",
                    object_type="AGENT_TASK",
                    object_id=str(task.task_id),
                    request_id=request_id,
                    before_hash=None,
                    after_hash=content_hash,
                    result="SUCCESS",
                    event_time=self._clock(),
                    metadata={"task_run_id": str(task.task_run_id)},
                )
            )
            await uow.commit()

        return RegisterAgentTaskResult(str(task.task_id), content_hash, created=True)
