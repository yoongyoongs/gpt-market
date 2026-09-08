"""候选扫描落库与查询（设计 §35，Step 14/17-18）。

写入：CandidatePipelineResult → scan_runs + candidate_snapshots +
expert_recall_rows + pareto_result_rows（bulk insert）；
mature 回填 → outcome_labels（upsert）+ miss_audit_rows + shadow_pool_rows。
查询：latest / funnel / experts / pareto / top(stage) / trace(code) /
trace_views（落库路径重建 TraceView）供 Step 15 API 与成熟编排消费。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.application.trace_view import TraceView
from app.v3.domain.candidate_engine import (
    CandidatePipelineResult,
    MissAuditEntry,
    OutcomeLabelResult,
    ShadowSampleEntry,
    STAGE_ORDER,
)
from app.v3.infrastructure.db.models import (
    BarSeriesRevisionModel,
    CandidateSnapshotModel,
    ExpertRecallRowModel,
    MarketBarModel,
    MissAuditRowModel,
    OutcomeLabelModel,
    ParetoResultRowModel,
    ScanRunModel,
    ShadowPoolRowModel,
)

__all__ = ["SQLAlchemyScanRepository"]

FEATURE_VERSION = "v1"

# asyncpg 单条语句参数上限 32767；明细行最多 11 列 → 每批 ≤2978 行，取 2000 留余。
_INSERT_BATCH = 2000


class SQLAlchemyScanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def _insert_batched(self, model, rows: list[dict]) -> None:
        for i in range(0, len(rows), _INSERT_BATCH):
            await self._session.execute(pg_insert(model).values(rows[i:i + _INSERT_BATCH]))

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
            await self._insert_batched(CandidateSnapshotModel, snapshot_rows)

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
            await self._insert_batched(ExpertRecallRowModel, expert_rows)

        pareto_rows = [
            {
                "row_id": uuid4(),
                "scan_run_id": scan_run_id,
                "code": entry.code,
                "front": entry.front,
                "protected": entry.protected,
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
            await self._insert_batched(ParetoResultRowModel, pareto_rows)
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
        result = await self._session.execute(
            select(
                ShadowPoolRowModel.sample_group,
                func.count(ShadowPoolRowModel.row_id),
            )
            .where(ShadowPoolRowModel.scan_run_id == scan_run_id)
            .group_by(ShadowPoolRowModel.sample_group)
        )
        return {group: count for group, count in result.all()}

    # ------------------------------------------------------------------
    # Step 17-18：mature 回填
    # ------------------------------------------------------------------

    async def bars_from(
        self,
        security_ids: list[UUID],
        since: datetime,
        *,
        limit: int = 21,
    ) -> dict[UUID, list[tuple[datetime, float, float, float]]]:
        """批量拉 QFQ 日K 中 since 之后的 ≤limit 根（含 T 日首根，用于 close_T 基准）。

        一次 SQL 覆盖全 universe；revision 取每股市最新 PUBLISHED DAY/QFQ。
        返回 (bar_time, high, low, close)，按时间升序；第一根若为 T 日收盘
        则作为 close_T，其余为观察窗。
        """
        if not security_ids:
            return {}
        ranked = (
            select(
                BarSeriesRevisionModel.revision_id,
                BarSeriesRevisionModel.security_id,
                func.row_number()
                .over(
                    partition_by=BarSeriesRevisionModel.security_id,
                    order_by=(
                        BarSeriesRevisionModel.known_at.desc(),
                        BarSeriesRevisionModel.revision_id.desc(),
                    ),
                )
                .label("rn"),
            )
            .where(
                BarSeriesRevisionModel.security_id.in_(security_ids),
                BarSeriesRevisionModel.period == "DAY",
                BarSeriesRevisionModel.adjust_type == "QFQ",
                BarSeriesRevisionModel.status == "PUBLISHED",
            )
            .subquery()
        )
        window_end = since + timedelta(days=limit * 3 + 14)
        result = await self._session.execute(
            select(
                ranked.c.security_id,
                MarketBarModel.bar_time,
                MarketBarModel.high,
                MarketBarModel.low,
                MarketBarModel.close,
            )
            .join(MarketBarModel, MarketBarModel.revision_id == ranked.c.revision_id)
            .where(
                ranked.c.rn == 1,
                MarketBarModel.bar_time > since,
                MarketBarModel.bar_time < window_end,
            )
            .order_by(ranked.c.security_id, MarketBarModel.bar_time)
        )
        bars: dict[UUID, list[tuple[datetime, float, float, float]]] = {}
        for security_id, bar_time, high, low, close in result.all():
            series = bars.setdefault(security_id, [])
            if len(series) >= limit:
                continue
            series.append((bar_time, float(high), float(low), float(close)))
        return bars

    async def trace_views(self, scan_run_id: UUID) -> list[TraceView]:
        """落库路径：从 snapshots + pareto + expert rows 重建 TraceView（mature 用）。"""
        snapshot_rows = await self.snapshots(scan_run_id, limit=1_000_000)
        pareto_by_code = {
            row.code: row for row in await self.pareto_rows(scan_run_id, limit=100_000)
        }
        expert_ranks: dict[str, dict[str, int]] = {}
        for row in await self.expert_rows(scan_run_id, limit=1_000_000):
            ranks = expert_ranks.setdefault(row.code, {})
            if row.expert not in ranks or row.rank < ranks[row.expert]:
                ranks[row.expert] = row.rank

        staged: dict[UUID, dict[str, CandidateSnapshotModel]] = {}
        for row in snapshot_rows:
            staged.setdefault(row.security_id, {})[row.stage] = row

        views: list[TraceView] = []
        for security_id, stages in staged.items():
            first = next(iter(stages.values()))
            # P0-09：阶段行按业务序（TRACE_STAGES）比较，不按字符串字典序
            # （DEEP<FINAL<MACHINE 字典序≠业务序）。
            alive_stages = [row for row in stages.values() if row.alive]
            dead_rows = [row for row in stages.values() if not row.alive]
            dead = (
                min(dead_rows, key=lambda row: STAGE_ORDER.get(row.stage, len(STAGE_ORDER)))
                if dead_rows else None
            )
            machine = stages.get("MACHINE")
            pareto_snapshot = stages.get("PARETO")
            final_row = stages.get("FINAL")
            pareto = pareto_by_code.get(first.code)
            views.append(TraceView(
                code=first.code,
                security_id=security_id,
                last_alive_stage=(
                    max(alive_stages, key=lambda row: STAGE_ORDER.get(row.stage, -1)).stage
                    if alive_stages else None
                ),
                drop_stage=dead.stage if dead else None,
                drop_reason=dead.drop_reason if dead else None,
                # P0-08 联动：Machine 淘汰行也带真实 rank，不限 alive
                machine_rank=machine.rank if machine is not None else None,
                machine_selected=bool(machine and machine.alive),
                pareto_front=pareto.front if pareto else None,
                pareto_selected=bool(pareto_snapshot and pareto_snapshot.alive),
                pareto_protected=pareto.protected if pareto else False,
                expert_ranks=expert_ranks.get(first.code, {}),
                final=bool(final_row and final_row.alive),
            ))
        views.sort(key=lambda view: view.code)
        return views

    async def save_outcome_labels(
        self, scan_run_id: UUID, labels: list[OutcomeLabelResult]
    ) -> int:
        """按 (scan_run_id, code) upsert；重跑 mature 覆盖旧值。"""
        if not labels:
            return 0
        rows = [
            {
                "row_id": uuid4(),
                "scan_run_id": scan_run_id,
                "code": entry.code,
                "mfe_5": _decimal(entry.mfe_5),
                "mfe_10": _decimal(entry.mfe_10),
                "mfe_20": _decimal(entry.mfe_20),
                "mae_5": _decimal(entry.mae_5),
                "mae_10": _decimal(entry.mae_10),
                "mae_20": _decimal(entry.mae_20),
                "time_to_8": entry.time_to_8,
                "time_to_10": entry.time_to_10,
                "time_to_15": entry.time_to_15,
                "close_t": _decimal(entry.close_t),
                "bars_used": entry.bars_used,
                "label": entry.label,
            }
            for entry in labels
        ]
        for i in range(0, len(rows), _INSERT_BATCH):
            stmt = pg_insert(OutcomeLabelModel).values(rows[i:i + _INSERT_BATCH])
            await self._session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["scan_run_id", "code"],
                    set_={
                        "mfe_5": stmt.excluded.mfe_5,
                        "mfe_10": stmt.excluded.mfe_10,
                        "mfe_20": stmt.excluded.mfe_20,
                        "mae_5": stmt.excluded.mae_5,
                        "mae_10": stmt.excluded.mae_10,
                        "mae_20": stmt.excluded.mae_20,
                        "time_to_8": stmt.excluded.time_to_8,
                        "time_to_10": stmt.excluded.time_to_10,
                        "time_to_15": stmt.excluded.time_to_15,
                        "close_t": stmt.excluded.close_t,
                        "bars_used": stmt.excluded.bars_used,
                        "label": stmt.excluded.label,
                    },
                )
            )
        return len(rows)

    async def save_miss_rows(self, scan_run_id: UUID, entries: list[MissAuditEntry]) -> int:
        """幂等重算：先清同 run 旧行再插入（backfill 语义）。"""
        if entries:
            await self._session.execute(
                delete(MissAuditRowModel).where(MissAuditRowModel.scan_run_id == scan_run_id)
            )
        rows = [
            {
                "row_id": uuid4(),
                "scan_run_id": scan_run_id,
                "code": entry.code,
                "future_label": entry.future_label,
                "last_alive_stage": entry.last_alive_stage or "UNIVERSE",
                "drop_stage": entry.drop_stage or "UNIVERSE",
                "drop_reason": entry.drop_reason or "",
                "audit_json": entry.audit,
            }
            for entry in entries
        ]
        if rows:
            await self._session.execute(pg_insert(MissAuditRowModel).values(rows))
        return len(rows)

    async def save_shadow_rows(
        self,
        scan_run_id: UUID,
        entries: list[ShadowSampleEntry],
        labels_by_code: dict[str, str | None] | None = None,
    ) -> int:
        """幂等重算：先清同 run 旧行再插入；labels_by_code 由 mature 提供联表值。"""
        if entries:
            await self._session.execute(
                delete(ShadowPoolRowModel).where(ShadowPoolRowModel.scan_run_id == scan_run_id)
            )
        labels_by_code = labels_by_code or {}
        rows = [
            {
                "row_id": uuid4(),
                "scan_run_id": scan_run_id,
                "code": entry.code,
                "sample_group": entry.sample_group,
                "drop_stage": entry.drop_stage or "UNIVERSE",
                "drop_reason": entry.drop_reason or "",
                "outcome_label": labels_by_code.get(entry.code),
            }
            for entry in entries
        ]
        if rows:
            await self._session.execute(pg_insert(ShadowPoolRowModel).values(rows))
        return len(rows)

    async def outcome_labels(self, scan_run_id: UUID) -> list[OutcomeLabelModel]:
        result = await self._session.execute(
            select(OutcomeLabelModel).where(OutcomeLabelModel.scan_run_id == scan_run_id)
        )
        return list(result.scalars().all())

    async def labels_by_code(self, scan_run_id: UUID) -> dict[str, str | None]:
        rows = await self.outcome_labels(scan_run_id)
        return {row.code: row.label for row in rows}

    async def shadow_rows(self, scan_run_id: UUID) -> list[ShadowPoolRowModel]:
        result = await self._session.execute(
            select(ShadowPoolRowModel).where(ShadowPoolRowModel.scan_run_id == scan_run_id)
        )
        return list(result.scalars().all())

    async def security_keys(self) -> dict[UUID, str]:
        """security_id → code 全量映射（universe 成员与特征行 join 用）。"""
        from app.v3.infrastructure.db.models import SecurityModel

        result = await self._session.execute(
            select(SecurityModel.security_id, SecurityModel.code)
        )
        return {security_id: code for security_id, code in result.all()}


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))
