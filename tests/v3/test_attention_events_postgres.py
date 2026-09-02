"""RT-04：AttentionEvent PostgreSQL 集成（真实 PG，migration 0016）。

- save → 读回字段完整（含 facts/source_snapshot_ids）；
- 相同 content_hash 重复提交 → 幂等；
- last_known_at 按 dedupe_key 取最近（冷却查询）；
- open_events 按 code / entry_plan / event_type 过滤；
- update_status 流转 OPEN → ACKED。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.v3.domain.attention import (
    AttentionEventType,
    AttentionStatus,
    IntradayAttentionEvent,
)
from app.v3.domain.hashing import canonical_hash
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc)


def _event(**overrides) -> IntradayAttentionEvent:
    values = dict(
        subject_type="ENTRY_PLAN", security_id=uuid4(),
        code="000001", market="SZ", entry_plan_id=uuid4(),
        event_type=AttentionEventType.STOP_HIT, severity="CRITICAL",
        facts={"last_price": 8.9, "stop_loss": 9.0},
        as_of=NOW, known_at=NOW,
        source_snapshot_ids=["snap-1"],
        status=AttentionStatus.OPEN,
        dedupe_key=f"STOP_HIT:{uuid4()}",
        content_hash=canonical_hash({"k": str(uuid4())}),
    )
    values.update(overrides)
    return IntradayAttentionEvent(**values)


async def test_save_and_read_back_roundtrip() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    event = _event()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        saved = await uow.attention.save(event)
        assert saved.attention_event_id == event.attention_event_id
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        rows = await uow.attention.open_events(entry_plan_id=event.entry_plan_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == AttentionEventType.STOP_HIT
    assert row.severity == "CRITICAL"
    assert row.facts["stop_loss"] == 9.0
    assert row.source_snapshot_ids == ["snap-1"]
    assert row.status == AttentionStatus.OPEN
    await engine.dispose()


async def test_same_content_hash_is_idempotent() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    event = _event()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.attention.save(event)
        await uow.attention.save(event)  # 重复提交：幂等
        await uow.commit()
        rows = await uow.attention.open_events(entry_plan_id=event.entry_plan_id)
    assert len(rows) == 1
    await engine.dispose()


async def test_last_known_at_returns_latest_per_dedupe_key() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    key = f"STOP_HIT:{uuid4()}"
    nonce = str(uuid4())  # content_hash 全局唯一，避免与历史运行残留冲突
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.attention.save(_event(
            dedupe_key=key, known_at=NOW,
            content_hash=canonical_hash({"n": nonce, "i": 1}),
        ))
        await uow.attention.save(_event(
            dedupe_key=key, known_at=NOW + timedelta(minutes=10),
            content_hash=canonical_hash({"n": nonce, "i": 2}),
        ))
        # 其它 dedupe_key 不应干扰
        await uow.attention.save(_event(
            dedupe_key=f"STOP_HIT:{uuid4()}", known_at=NOW + timedelta(hours=1),
            content_hash=canonical_hash({"n": nonce, "i": 3}),
        ))
        await uow.commit()
        last = await uow.attention.last_known_at(key)
    assert last == NOW + timedelta(minutes=10)
    await engine.dispose()


async def test_open_events_filter_by_entry_plan_and_status_flow() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    plan_id = uuid4()
    event = _event(entry_plan_id=plan_id)
    other = _event()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.attention.save(event)
        await uow.attention.save(other)
        await uow.commit()
        rows = await uow.attention.open_events(entry_plan_id=plan_id)
        assert len(rows) == 1
        # OPEN → ACKED 后不再出现在 open_events
        await uow.attention.update_status(
            event.attention_event_id, AttentionStatus.ACKED
        )
        await uow.commit()
        rows = await uow.attention.open_events(codes=["000001"])
    assert all(row.attention_event_id != event.attention_event_id for row in rows)
    await engine.dispose()
