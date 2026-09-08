"""Step 14：候选扫描落库 PostgreSQL 集成（migration 0018）。

pipeline result → scans.save_scan → 读回 scan_run / snapshots /
expert_rows / pareto_rows / miss_rows / shadow_group_counts。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)


def _pipeline_result():
    from test_candidate_pipeline_trace import _run

    result, _ = _run()
    return result


async def test_save_scan_and_read_back() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    result = _pipeline_result()

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        scan_run_id = await uow.scans.save_scan(result)
        await uow.commit()

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        run = await uow.scans.run_by_id(scan_run_id)
        assert run is not None
        assert run.status == "PUBLISHED"
        assert run.universe_count == 10
        assert run.final_count == len(result.final_entries)

        snapshots = await uow.scans.snapshots(scan_run_id, code="000003")
        assert snapshots
        assert all(row.stage == "SAFETY" and not row.alive for row in snapshots)
        assert "ST" in (snapshots[0].drop_reason or "")

        final_rows = await uow.scans.snapshots(
            scan_run_id, stage="FINAL", alive_only=True, limit=30,
        )
        assert len(final_rows) == len(result.final_entries)

        expert_rows = await uow.scans.expert_rows(scan_run_id)
        assert expert_rows, "expert recall rows should be persisted"
        assert {row.expert for row in expert_rows} <= {
            "LP", "BT", "RV", "AC", "PB", "RS", "FQ", "CAT",
        }

        pareto_rows = await uow.scans.pareto_rows(scan_run_id)
        assert len(pareto_rows) == len(result.pareto.entries)
        assert all(row.front >= 1 for row in pareto_rows)

        assert await uow.scans.miss_rows(scan_run_id) == []
        assert await uow.scans.shadow_group_counts(scan_run_id) == {}

    await engine.dispose()
