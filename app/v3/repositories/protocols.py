from __future__ import annotations

from types import TracebackType
from typing import Protocol

from app.v3.contracts.agent import AgentTask
from app.v3.domain.audit import AuditEvent


class AgentTaskRepository(Protocol):
    async def add_if_absent(self, task: AgentTask) -> bool: ...


class AuditRepository(Protocol):
    async def add(self, event: AuditEvent) -> None: ...


class UnitOfWork(Protocol):
    tasks: AgentTaskRepository
    audits: AuditRepository

    async def __aenter__(self) -> "UnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
