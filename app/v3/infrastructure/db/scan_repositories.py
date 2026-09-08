"""候选扫描落库与查询（设计 §35，Step 14）。

写入：CandidatePipelineResult → scan_runs + candidate_snapshots +
expert_recall_rows + pareto_result_rows（bulk insert）。
查询：latest / funnel / experts / pareto / top(stage) / trace(code)，
供 Step 15 API 直接消费。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.domain.candidate_engine import CandidatePipelineResult
from app.v3.infrastructure.db.models import (
    CandidateSnapshotModel,
    ExpertRecallRowModel,
    MissAuditRowModel,
    ParetoResultRowModel,
    ScanRunModel,
    ShadowPoolRowModel,
)

__all__ = ["SQLAlchemyScanRepository"]

FEATURE_VERSION = "v1"


class SQLAlchemyScanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def save_scan(self, result: CandidatePipelineResult, *, scan_time: datetime | None = None) -> UUID:
        """一次扫描的三类明细一次性落库（事务由 UoW 提交）。"""
        funnel = result.funnel
        counts = {stage.stage: stage.output_count for stage in funnel.stages}
        scan_run_id = result.scan_id
        await self._session.execute(
            pg_insert(ScanRunModel).values(
                scan_run_id=scan_run_id,
                scan_time=scan_time or datetime.utcnow(),
                market_date=result.trade_date,
                strategy_version=result.strategy_version,
                parameter_version=result.parameter_version,
                universe_count=counts.get("UNIVERSE", 0),
                eligible_count=counts.get("SAFETY", 0),
                recall_count=counts.get("RECALL", 0),
                pareto_count=counts.get("PARETO", 0),
                machine_count=counts.get("MACHINE", 0),
                deep_count=counts.get("DEEP", 0),
                final_count=counts.get("FINAL", 0),
                duration_ms=sum(stage.duration_ms for stage in funnel.stages),
                status="PUBLISHED",
            )
        )

        snapshot_rows = []
        for trace in result.trace.traces:
            data_timestamp = None
            for record in trace.records:
                if record.stage == "UNIVERSE":
                    continue
                alive = record.alive
                snapshot_rows.append({
                    "snapshot_id": uuid4(),
                    "scan_run_id": scan_run_id,
                    "security_id": trace.security_id,
                    "code": trace.code,
                    "stage": record.stage,
                    "alive": alive,
                    "score": None if record.score is None else Decimal(str(record.score)),
                    "rank": record.rank,
                    "drop_reason": record.drop_reason,
                    "feature_version": FEATURE_VERSION,
                    "data_timestamp": data_timestamp,
                })
        if snapshot_rows:
            await self._session.execute(pg_insert(CandidateSnapshotModel).values(snapshot_rows))

        expert_rows = []
        for entry in result.union.entries:
            for hit in entry.hits:
                expert_rows.append({
                    "row_id": uuid4(),
                    "scan_run_id": scan_run_id,
                    "code": hit.code,
                    "expert": hit.expert,
                    "score": Decimal(str(hit.score)),
                    "rank": hit.rank,
                    "hit": True,
                    "reason_json": {"reasons": list(hit.reasons), "confidence": hit.confidence},
                })
        if expert_rows:
            await self._session.execute(pg_insert(ExpertRecallRowModel).values(expert_rows))

        pareto_rows = [
            {
                "row_id": uuid4(),
                "scan_run_id": scan_run_id,
                "code": entry.code,
                "front": entry.front,
                "crowding_distance": Decimal(str(min(entry.crowding, 1e12))),
                "p_position": _decimal(entry.scores.get("position")),
                "p_transition": _decimal(entry.scores.get("transition")),
                "p_accumulation": _decimal(entry.scores.get("accumulation")),
                "p_quality_catalyst": _decimal(entry.scores.get("quality_catalyst")),
                "p_risk_reward": _decimal(entry.scores.get("risk_reward")),
            }
            for entry in result.pareto.entries
        ]
        if pareto_rows:
            await self._session.execute(pg_insert(ParetoResultRowModel).values(pareto_rows))
        return scan_run_id

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def latest_run(self) -> ScanRunModel | None:
        result = await self._session.execute(
            select(ScanRunModel)
            .where(ScanRunModel.status == "PUBLISHED")
            .order_by(ScanRunModel.market_date.desc(), ScanRunModel.scan_time.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def run_by_id(self, scan_run_id: UUID) -> ScanRunModel | None:
        result = await self._session.execute(
            select(ScanRunModel).where(ScanRunModel.scan_run_id == scan_run_id)
        )
        return result.scalar_one_or_none()

    async def snapshots(
        self,
        scan_run_id: UUID,
        *,
        stage: str | None = None,
        code: str | None = None,
        alive_only: bool = False,
        limit: int = 200,
    ) -> list[CandidateSnapshotModel]:
        stmt = select(CandidateSnapshotModel).where(
            CandidateSnapshotModel.scan_run_id == scan_run_id
        )
        if stage is not None:
            stmt = stmt.where(CandidateSnapshotModel.stage == stage)
        if code is not None:
            stmt = stmt.where(CandidateSnapshotModel.code == code)
        if alive_only:
            stmt = stmt.where(CandidateSnapshotModel.alive.is_(True))
        stmt = stmt.order_by(CandidateSnapshotModel.stage, CandidateSnapshotModel.rank.nulls_last(), CandidateSnapshotModel.code)
        result = await self._session.execute(stmt.limit(limit))
        return list(result.scalars().all())

    async def expert_rows(self, scan_run_id: UUID, *, expert: str | None = None, limit: int = 5000) -> list[ExpertRecallRowModel]:
        stmt = select(ExpertRecallRowModel).where(
            ExpertRecallRowModel.scan_run_id == scan_run_id
        )
        if expert is not None:
            stmt = stmt.where(ExpertRecallRowModel.expert == expert)
        result = await self._session.execute(stmt.limit(limit))
        return list(result.scalars().all())

    async def pareto_rows(self, scan_run_id: UUID, *, limit: int = 5000) -> list[ParetoResultRowModel]:
        result = await self._session.execute(
            select(ParetoResultRowModel)
            .where(ParetoResultRowModel.scan_run_id == scan_run_id)
            .order_by(ParetoResultRowModel.front, ParetoResultRowModel.code)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def miss_rows(self, scan_run_id: UUID, *, limit: int = 50) -> list:
        result = await self._session.execute(
            select(MissAuditRowModel)
            .where(MissAuditRowModel.scan_run_id == scan_run_id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def shadow_group_counts(self, scan_run_id: UUID) -> dict[str, int]:
        from sqlalchemy import func

        result = await self._session.execute(
            select(
                ShadowPoolRowModel.sample_group,
                func.count(ShadowPoolRowModel.row_id),
            )
            .where(ShadowPoolRowModel.scan_run_id == scan_run_id)
            .group_by(ShadowPoolRowModel.sample_group)
        )
        return {group: count for group, count in result.all()}


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))
