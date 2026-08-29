from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.register_agent_task import RegisterAgentTaskService
from app.v3.contracts.agent import AgentTask, Subject, SubjectType
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured")


@pytest.fixture
async def connection():
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as value:
        yield value
        await value.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_phase1_schema_and_immutable_raw_document(connection) -> None:
    table_names = {
        row[0]
        for row in (
            await connection.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='v3'")
            )
        )
    }
    assert {
        "evidence_sources",
        "raw_documents",
        "evidence_records",
        "task_profiles",
        "expected_runs",
        "task_runs",
        "agent_tasks",
        "ai_result_envelopes",
        "audit_events",
    } <= table_names

    source_id = uuid4()
    document_id = uuid4()
    now = datetime.now(timezone.utc)
    await connection.execute(
        text(
            "INSERT INTO v3.evidence_sources "
            "(evidence_source_id, code, source_type) VALUES (:id, :code, 'OFFICIAL')"
        ),
        {"id": source_id, "code": f"fixture-{source_id}"},
    )
    await connection.execute(
        text(
            "INSERT INTO v3.raw_documents "
            "(raw_document_id, evidence_source_id, raw_reference, fetch_time, known_at, content_hash) "
            "VALUES (:id, :source_id, 'fixture', :fetch_time, :known_at, :content_hash)"
        ),
        {
            "id": document_id,
            "source_id": source_id,
            "fetch_time": now,
            "known_at": now,
            "content_hash": document_id.hex + document_id.hex,
        },
    )
    await connection.commit()

    with pytest.raises(DBAPIError, match="immutable V3 record"):
        await connection.execute(
            text("UPDATE v3.raw_documents SET raw_reference='changed' WHERE raw_document_id=:id"),
            {"id": document_id},
        )
    await connection.rollback()


@pytest.mark.asyncio
async def test_task_group_count_constraint_rejects_inconsistent_state(connection) -> None:
    profile_id = uuid4()
    await connection.execute(
        text(
            "INSERT INTO v3.task_profiles "
            "(task_profile_id, profile_code, version, timezone, context_level, output_schema, content_hash) "
            "VALUES (:id, :code, 1, 'Asia/Shanghai', 'NORMAL', '{}'::jsonb, :content_hash)"
        ),
        {"id": profile_id, "code": f"fixture-{profile_id}", "content_hash": profile_id.hex + profile_id.hex},
    )
    await connection.commit()

    with pytest.raises(IntegrityError, match="ck_task_runs_group_count_total"):
        await connection.execute(
            text(
                "INSERT INTO v3.task_runs "
                "(task_run_id, task_profile_id, expected_group_count, successful_group_count, "
                "failed_group_count, pending_group_count) VALUES (:id, :profile_id, 30, 29, 0, 0)"
            ),
            {"id": uuid4(), "profile_id": profile_id},
        )
    await connection.rollback()


@pytest.mark.asyncio
async def test_agent_task_audit_commit_idempotency_rollback_and_concurrency() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    profile_id = uuid4()
    task_run_id = uuid4()
    now = datetime.now(timezone.utc)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.task_profiles "
                "(task_profile_id, profile_code, version, timezone, context_level, output_schema, content_hash) "
                "VALUES (:id, :code, 1, 'Asia/Shanghai', 'NORMAL', '{}'::jsonb, :content_hash)"
            ),
            {"id": profile_id, "code": f"uow-{profile_id}", "content_hash": profile_id.hex * 2},
        )
        await connection.execute(
            text(
                "INSERT INTO v3.task_runs "
                "(task_run_id, task_profile_id, expected_group_count, pending_group_count) "
                "VALUES (:id, :profile_id, 1, 1)"
            ),
            {"id": task_run_id, "profile_id": profile_id},
        )

    task = AgentTask(
        task_id=uuid4(),
        task_run_id=task_run_id,
        task_type="STOCK_REVIEW",
        subject=Subject(type=SubjectType.STOCK, code="600000"),
        task_profile="stock-review-v1",
        trigger_type="INTEGRATION_TEST",
        as_of=now,
        context_pack_id=uuid4(),
        context_pack_hash="a" * 64,
        expected_result_type="STOCK_REVIEW",
        constraints={"correlation_id": uuid4()},
    )
    service = RegisterAgentTaskService(lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: now)

    first = await service.execute(task, actor_type="SYSTEM", request_id=f"test-{task.task_id}")
    second = await service.execute(task, actor_type="SYSTEM", request_id=f"test-{task.task_id}")
    assert first.created is True
    assert second.created is False

    rejected_task = task.model_copy(update={"task_id": uuid4()})
    with pytest.raises(ValidationError, match="actor_type"):
        await service.execute(rejected_task, actor_type="x" * 33)

    conflicting_task = task.model_copy(update={"task_type": "DIFFERENT_REVIEW"})
    with pytest.raises(IntegrityError):
        await service.execute(conflicting_task, actor_type="SYSTEM")

    concurrent_task = task.model_copy(update={"task_id": uuid4(), "context_pack_id": uuid4()})
    concurrent_results = await asyncio.gather(
        *(
            service.execute(concurrent_task, actor_type="SYSTEM", request_id=f"concurrent-{index}")
            for index in range(8)
        )
    )
    assert sum(result.created for result in concurrent_results) == 1

    async with engine.connect() as connection:
        task_count = (
            await connection.execute(
                text("SELECT count(*) FROM v3.agent_tasks WHERE task_run_id=:task_run_id"),
                {"task_run_id": task_run_id},
            )
        ).scalar_one()
        audit_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM v3.audit_events "
                    "WHERE object_type='AGENT_TASK' AND object_id=:object_id"
                ),
                {"object_id": str(task.task_id)},
            )
        ).scalar_one()
        concurrent_audit_count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM v3.audit_events "
                    "WHERE object_type='AGENT_TASK' AND object_id=:object_id"
                ),
                {"object_id": str(concurrent_task.task_id)},
            )
        ).scalar_one()
    await engine.dispose()

    assert task_count == 2
    assert audit_count == 1
    assert concurrent_audit_count == 1
