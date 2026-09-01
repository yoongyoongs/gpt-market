"""RC-08A Audit Helper 真实 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.audit_helper import AuditRecorder
from app.v3.application.manage_strategy import StrategyStabilizationService
from app.v3.domain.audit import AuditEvent
from app.v3.domain.strategy import StrategyVersionCreate
from app.v3.infrastructure.db.models import AuditEventModel
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 8, 30, 8, tzinfo=timezone.utc)


async def test_recorder_persists_audit_event_in_real_transaction() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    object_id = uuid4()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        audit_id = await AuditRecorder(uow, clock=lambda: NOW).record(
            action="AGENT_TASK_REGISTERED", object_type="AGENT_TASK",
            object_id=str(object_id), actor_type="HUMAN", actor_id="op-1",
            request_id="req-pg", before={"v": 1}, after={"v": 2},
        )
        await uow.commit()
    async with sessions() as session:
        row = await session.get(AuditEventModel, audit_id)
    assert row is not None
    assert row.action == "AGENT_TASK_REGISTERED"
    assert row.request_id == "req-pg"
    assert row.before_hash and row.after_hash
    await engine.dispose()


async def test_strategy_version_service_writes_business_and_audit_together() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    command = StrategyVersionCreate(
        strategy_code=f"audit-pg-{uuid4().hex[:8]}", version=1,
        configuration={"k": 1}, rationale="audit integration", created_by="op-2",
    )
    service = StrategyStabilizationService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW,
    )
    report = await service.add_strategy_version(command, request_id="req-sv")
    async with sessions() as session:
        rows = (await session.scalars(select(AuditEventModel).where(
            AuditEventModel.action == "STRATEGY_VERSION_APPENDED",
            AuditEventModel.object_id == str(report["strategy_version_id"]),
        ))).all()
    assert len(rows) == 1
    assert rows[0].actor_id == "op-2"
    assert rows[0].metadata_payload["strategy_code"] == command.strategy_code
    await engine.dispose()


async def test_audit_event_domain_contract_roundtrip() -> None:
    event = AuditEvent(
        audit_id=uuid4(), actor_type="HUMAN", actor_id="op-3",
        action="X", object_type="Y", object_id="z",
        result="SUCCESS", event_time=NOW,
    )
    assert event.before_hash is None and event.after_hash is None
