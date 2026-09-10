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


async def test_mature_backfill_pending_and_read_back() -> None:
    """mature 编排（无 bars → 全 PENDING）+ 落库路径 TraceView 一致性。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.v3.application.mature_scan_outcomes import MatureScanOutcomesService
    from app.v3.application.trace_view import to_trace_views
    from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    result = _pipeline_result()

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        scan_run_id = await uow.scans.save_scan(result)
        await uow.commit()

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        # 落库路径 TraceView 与内存路径一致
        db_views = {v.code: v for v in await uow.scans.trace_views(scan_run_id)}
        mem_views = {v.code: v for v in to_trace_views(result)}
        assert set(db_views) == set(mem_views)
        for code, db_view in db_views.items():
            mem_view = mem_views[code]
            assert db_view.final == mem_view.final
            assert db_view.drop_stage == mem_view.drop_stage
            assert db_view.machine_rank == mem_view.machine_rank
            assert db_view.pareto_front == mem_view.pareto_front
            assert db_view.pareto_protected == mem_view.pareto_protected
            assert db_view.expert_ranks == mem_view.expert_ranks
            # P0-11：RECALL/DEEP 行 rank 双路径一致（union RRF 名次/Deep 名次）
            assert db_view.recall_rank == mem_view.recall_rank
            assert db_view.deep_rank == mem_view.deep_rank

        # mature：测试库无 bars → 全 PENDING，但 label 行要覆盖全 universe
        summary = await MatureScanOutcomesService().execute(
            uow.scans, scan_id=scan_run_id,
        )
        await uow.commit()
        assert summary["status"] == "ok"
        assert summary["labels_upserted"] == len(result.trace.traces)
        # R2.1-P0-03：无 bar → 全 PENDING；matured=0（旧键 labeled 已删）
        assert summary["pending"] == len(result.trace.traces)
        assert summary["matured"] == 0

        label_rows = await uow.scans.outcome_labels(scan_run_id)
        assert len(label_rows) == len(result.trace.traces)
        assert all(row.label is None and row.bars_used == 0 for row in label_rows)

        # 幂等：重跑不翻倍
        summary2 = await MatureScanOutcomesService().execute(
            uow.scans, scan_id=scan_run_id,
        )
        await uow.commit()
        assert summary2["labels_upserted"] == summary["labels_upserted"]
        assert len(await uow.scans.outcome_labels(scan_run_id)) == len(label_rows)

        # shadow：dead 股按分层落库（数量与内存 sample 一致）
        from app.v3.application.shadow_pool import ShadowPoolService

        expected_shadow = ShadowPoolService().sample(list(db_views.values()))
        assert summary["shadow_rows"] == len(expected_shadow)
        counts = await uow.scans.shadow_group_counts(scan_run_id)
        assert sum(counts.values()) == len(expected_shadow)

    await engine.dispose()


async def test_bars_from_includes_t_day_boundary() -> None:
    """R2.1-P0-02：bars_from 用 >= 含 T 日首根（bar_time==since）。

    旧实现 > 把 T 日 K 排除 → _split_t_day 拿不到 close_T → 全部 PENDING。
    """
    from datetime import timedelta
    from decimal import Decimal
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.v3.application.mature_scan_outcomes import (
        MatureScanOutcomesService,
        t_day_window,
    )
    from app.v3.infrastructure.db.models import (
        BarSeriesRevisionModel,
        MarketBarModel,
        SecurityModel,
    )
    from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    security_id = uuid4()
    revision_id = uuid4()
    market_date = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    since, t_ordinal = t_day_window(market_date)  # 上海 09-01 00:00（UTC）
    # T 日首根 bar_time 恰等于 since（00:00 口径）——旧 > 条件下必被排除
    bar_times = [since + timedelta(days=i) for i in range(21)]

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        uow._session.add(SecurityModel(
            security_id=security_id, code="900001", market="SH", name="边界测试",
        ))
        uow._session.add(BarSeriesRevisionModel(
            revision_id=revision_id, security_id=security_id,
            period="DAY", adjust_type="QFQ", source="test", upstream_source="test",
            raw_bar_available=True, point_in_time_precision="FULL",
            known_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            content_hash=f"r21-p002-{revision_id.hex}", status="PUBLISHED",
        ))
        for i, bar_time in enumerate(bar_times):
            price = Decimal("10.5") + i
            uow._session.add(MarketBarModel(
                revision_id=revision_id, bar_time=bar_time,
                open=price, high=price, low=price, close=price,
                volume=1000, amount=price * 1000,
                event_time=bar_time, fetch_time=bar_time,
            ))
        await uow.commit()

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        bars = await uow.scans.bars_from([security_id], since)
        assert bars[security_id][0][0] == since  # T 日首根在内
        assert len(bars[security_id]) == 21      # T + 20 观察窗

        close_t, future = MatureScanOutcomesService._split_t_day(
            bars[security_id], t_ordinal,
        )
        assert close_t == 10.5
        assert len(future) == 20

    await engine.dispose()
