from __future__ import annotations

from datetime import datetime, timezone
from types import TracebackType
from uuid import uuid4

import pytest

from app.v3.application.register_agent_task import RegisterAgentTaskService
from app.v3.contracts.agent import AgentTask, Subject, SubjectType


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def make_task() -> AgentTask:
    return AgentTask(
        task_id=uuid4(),
        task_run_id=uuid4(),
        task_type="STOCK_REVIEW",
        subject=Subject(type=SubjectType.STOCK, code="600000"),
        task_profile="stock-review-v1",
        trigger_type="MANUAL",
        as_of=NOW,
        context_pack_id=uuid4(),
        context_pack_hash="a" * 64,
        expected_result_type="STOCK_REVIEW",
    )


class FakeStore:
    def __init__(self) -> None:
        self.task_hashes: set[str] = set()
        self.audit_count = 0


class FakeTaskRepository:
    def __init__(self, store: FakeStore, pending_hashes: set[str]) -> None:
        self._store = store
        self._pending_hashes = pending_hashes

    async def add_if_absent(self, task: AgentTask) -> bool:
        content_hash = task.computed_content_hash()
        if content_hash in self._store.task_hashes or content_hash in self._pending_hashes:
            return False
        self._pending_hashes.add(content_hash)
        return True


class FakeAuditRepository:
    def __init__(self, uow: "FakeUnitOfWork", *, fail: bool) -> None:
        self._uow = uow
        self._fail = fail

    async def add(self, event) -> None:
        if self._fail:
            raise RuntimeError("audit write failed")
        self._uow.pending_audits += 1


class FakeUnitOfWork:
    def __init__(self, store: FakeStore, *, fail_audit: bool = False) -> None:
        self.store = store
        self.fail_audit = fail_audit
        self.pending_hashes: set[str] = set()
        self.pending_audits = 0
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> "FakeUnitOfWork":
        self.tasks = FakeTaskRepository(self.store, self.pending_hashes)
        self.audits = FakeAuditRepository(self, fail=self.fail_audit)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None or not self.committed:
            await self.rollback()

    async def commit(self) -> None:
        self.store.task_hashes.update(self.pending_hashes)
        self.store.audit_count += self.pending_audits
        self.committed = True

    async def rollback(self) -> None:
        self.pending_hashes.clear()
        self.pending_audits = 0
        self.rolled_back = True


@pytest.mark.asyncio
async def test_registers_task_and_audit_in_one_unit_of_work() -> None:
    store = FakeStore()
    uow = FakeUnitOfWork(store)
    service = RegisterAgentTaskService(lambda: uow, clock=lambda: NOW)

    result = await service.execute(make_task(), actor_type="SYSTEM", request_id="request-1")

    assert result.created is True
    assert uow.committed is True
    assert len(store.task_hashes) == 1
    assert store.audit_count == 1


@pytest.mark.asyncio
async def test_same_task_is_idempotent_without_duplicate_audit() -> None:
    store = FakeStore()
    task = make_task()
    first_uow = FakeUnitOfWork(store)
    second_uow = FakeUnitOfWork(store)
    units = iter((first_uow, second_uow))
    service = RegisterAgentTaskService(lambda: next(units), clock=lambda: NOW)

    first = await service.execute(task, actor_type="SYSTEM")
    second = await service.execute(task, actor_type="SYSTEM")

    assert first.created is True
    assert second.created is False
    assert store.audit_count == 1
    assert second_uow.rolled_back is True


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_task() -> None:
    store = FakeStore()
    uow = FakeUnitOfWork(store, fail_audit=True)
    service = RegisterAgentTaskService(lambda: uow, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="audit write failed"):
        await service.execute(make_task(), actor_type="SYSTEM")

    assert uow.rolled_back is True
    assert store.task_hashes == set()
    assert store.audit_count == 0
