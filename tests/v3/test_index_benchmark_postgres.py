from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.index_benchmark import (
    IndexBenchmarkBar,
    IndexBenchmarkRevision,
    IndexBenchmarkRevisionContent,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


def content(known_at: datetime, code: str = "HS300") -> IndexBenchmarkRevisionContent:
    bars = tuple(
        IndexBenchmarkBar(
            bar_time=known_at - timedelta(days=60 - index),
            close=3800 + index * 5 + (index % 4), amount=1e11 + index * 1e9,
        )
        for index in range(60)
    )
    return IndexBenchmarkRevisionContent(
        revision_id=uuid4(), benchmark_code=code, source="eastmoney",
        upstream_source="eastmoney", fetch_time=known_at, known_at=known_at, bars=bars,
    )


async def test_index_revisions_publish_dedup_and_point_in_time_read() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    first = IndexBenchmarkRevision.build(content(NOW - timedelta(hours=2)))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.index_benchmarks.publish(first) is True
        await uow.commit()
    # 相同内容重发：去重 False
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.index_benchmarks.publish(first) is False
        await uow.commit()
    # 更晚 known_at 的新 revision：可发布且按 as_of 点时读取
    second = IndexBenchmarkRevision.build(content(NOW - timedelta(hours=1)))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.index_benchmarks.publish(second) is True
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        early = await uow.index_benchmarks.latest("HS300", as_of=NOW - timedelta(hours=2))
        current = await uow.index_benchmarks.latest("HS300", as_of=NOW)
        missing = await uow.index_benchmarks.latest("CSI500", as_of=NOW)
    assert early.revision_id == first.revision_id
    assert current.revision_id == second.revision_id
    assert missing is None
    await engine.dispose()
