from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.domain.hashing import canonical_hash
from app.v3.domain.performance import (
    PerformanceAttributionCreate,
    RegressionCaseCreate,
    ReplayRunCreate,
    content_hash,
)
from app.v3.infrastructure.db.models import (
    BarSeriesRevisionModel,
    ContextPackModel,
    DecisionModel,
    EntryPlanModel,
    EvidenceRecordModel,
    PerformanceAttributionModel,
    PerformanceSummaryModel,
    RecallMissEvaluationModel,
    RecallMissRunModel,
    RegressionCaseModel,
    ReplayRunModel,
    TradeLedgerModel,
)
from app.v3.repositories.errors import RepositoryConflictError, RepositoryNotFoundError


class SQLAlchemyPerformanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_attribution(self, command: PerformanceAttributionCreate) -> UUID:
        if command.trade_id is not None:
            trade = await self._session.get(TradeLedgerModel, command.trade_id)
            if trade is None:
                raise RepositoryNotFoundError("attributed trade not found")
            if command.trade_bound_entry_plan_id != trade.entry_plan_id:
                raise RepositoryConflictError(
                    "trade-bound plan must equal the immutable plan binding on the trade"
                )
        if command.original_entry_plan_id is not None and command.decision_id is not None:
            plan = await self._session.get(EntryPlanModel, command.original_entry_plan_id)
            if plan is None or plan.decision_id != command.decision_id or plan.version != 1:
                raise RepositoryConflictError("original entry plan must be version 1 of decision")
        self._session.add(PerformanceAttributionModel(
            **command.model_dump(mode="python"), content_hash=content_hash(command)
        ))
        return command.attribution_id

    async def mature_decision_candidates(self, as_of: datetime) -> list[dict]:
        """RC-06A：成熟引擎候选 —— 含 original EntryPlan 的决策（produced_at <= as_of）。"""
        rows = (
            await self._session.scalars(
                select(DecisionModel).where(
                    DecisionModel.original_entry_plan_id.is_not(None),
                    DecisionModel.produced_at <= as_of,
                )
            )
        ).all()
        return [
            {
                "decision_id": row.decision_id,
                "security_id": row.security_id,
                "as_of": row.as_of,
                "produced_at": row.produced_at,
                "payload": dict(row.payload or {}),
                "original_entry_plan_id": row.original_entry_plan_id,
                "original_entry_plan_snapshot": dict(
                    row.original_entry_plan_snapshot or {}
                ),
            }
            for row in rows
        ]

    async def decision_trades(self, decision_id: UUID) -> list[dict]:
        """RC-06A：决策关联的已确认成交事实（Trade Ledger）。"""
        rows = (
            await self._session.scalars(
                select(TradeLedgerModel).where(
                    TradeLedgerModel.decision_id == decision_id
                ).order_by(TradeLedgerModel.trade_time)
            )
        ).all()
        return [
            {
                "trade_id": row.trade_id,
                "side": row.side,
                "trade_time": row.trade_time,
                "price": row.price,
                "quantity": row.quantity,
            }
            for row in rows
        ]

    async def attribution_exists(self, content_hash_value: str) -> bool:
        return (
            await self._session.scalar(
                select(
                    select(PerformanceAttributionModel.attribution_id)
                    .where(PerformanceAttributionModel.content_hash == content_hash_value)
                    .exists()
                )
            )
        ) is True

    async def run_replay(self, command: ReplayRunCreate) -> dict:
        checks: list[dict] = []
        revision_set = {"bars": [], "evidence": [], "contexts": []}
        await self._check_references(
            command.bar_revision_ids, BarSeriesRevisionModel, "revision_id", "bars",
            command.replay_as_of, checks, revision_set,
        )
        await self._check_references(
            command.evidence_ids, EvidenceRecordModel, "evidence_id", "evidence",
            command.replay_as_of, checks, revision_set,
        )
        await self._check_references(
            command.context_pack_ids, ContextPackModel, "context_pack_id", "contexts",
            command.replay_as_of, checks, revision_set,
        )
        blocked = any(not item["passed"] for item in checks)
        status = "BLOCKED" if blocked else "COMPLETED"
        result = (
            {"executed": False, "reason": "POINT_IN_TIME_LEAKAGE_OR_MISSING_INPUT"}
            if blocked else
            {"executed": True, "mode": "POINT_IN_TIME", "input_count": sum(map(len, revision_set.values()))}
        )
        payload = {
            "strategy_version": command.strategy_version,
            "replay_as_of": command.replay_as_of,
            "revision_set": revision_set,
            "parameters": command.parameters,
            "status": status,
            "leakage_checks": checks,
            "result": result,
        }
        self._session.add(ReplayRunModel(
            replay_run_id=command.replay_run_id,
            content_hash=canonical_hash(payload), **payload,
        ))
        return {"replay_run_id": command.replay_run_id, **payload}

    async def _check_references(
        self, ids, model, id_column: str, kind: str, replay_as_of: datetime,
        checks: list[dict], revision_set: dict,
    ) -> None:
        for object_id in ids:
            row = await self._session.get(model, object_id)
            if row is None:
                checks.append({"kind": kind, "id": str(object_id), "passed": False,
                               "reason": "MISSING_INPUT"})
                continue
            passed = row.known_at <= replay_as_of
            checks.append({
                "kind": kind, "id": str(object_id), "known_at": row.known_at.isoformat(),
                "replay_as_of": replay_as_of.isoformat(), "passed": passed,
                "reason": None if passed else "FUTURE_DATA_LEAKAGE",
            })
            if passed:
                revision_set[kind].append({
                    "id": str(getattr(row, id_column)), "known_at": row.known_at.isoformat(),
                    "content_hash": row.content_hash,
                })

    async def add_regression_case(self, command: RegressionCaseCreate) -> UUID:
        status = "ACTIVE"
        blocked_reason = None
        if command.source_replay_run_id is not None:
            replay = await self._session.get(ReplayRunModel, command.source_replay_run_id)
            if replay is None:
                raise RepositoryNotFoundError("source replay run not found")
            if replay.status == "BLOCKED":
                status = "BLOCKED"
                blocked_reason = "SOURCE_REPLAY_BLOCKED"
        self._session.add(RegressionCaseModel(
            **command.model_dump(mode="python"), status=status,
            blocked_reason=blocked_reason, content_hash=content_hash(command),
        ))
        return command.regression_case_id

    async def summarize(
        self, ability: str, regime_snapshot_id: UUID | None,
        strategy_version: str, window_start: datetime, window_end: datetime,
    ) -> dict:
        filters = [
            PerformanceAttributionModel.ability == ability,
            PerformanceAttributionModel.strategy_version == strategy_version,
            PerformanceAttributionModel.matures_at >= window_start,
            PerformanceAttributionModel.matures_at <= window_end,
        ]
        if regime_snapshot_id is None:
            filters.append(PerformanceAttributionModel.regime_snapshot_id.is_(None))
            regime_key = "UNKNOWN"
        else:
            filters.append(PerformanceAttributionModel.regime_snapshot_id == regime_snapshot_id)
            regime_key = str(regime_snapshot_id)
        rows = (await self._session.scalars(select(PerformanceAttributionModel).where(*filters))).all()
        def average(field):
            values = [float(getattr(item, field)) for item in rows if getattr(item, field) is not None]
            return sum(values) / len(values) if values else None
        metrics = {
            "average_raw_return": average("raw_return"),
            "average_excess_return": average("excess_return"),
            "average_mfe": average("mfe"), "average_mae": average("mae"),
            "target_hit_rate": sum(item.target_hit is True for item in rows) / len(rows) if rows else None,
            "stop_hit_rate": sum(item.stop_hit is True for item in rows) / len(rows) if rows else None,
        }
        source_ids = [str(item.attribution_id) for item in rows]
        payload = {"ability": ability, "regime_key": regime_key,
                   "strategy_version": strategy_version, "window_start": window_start,
                   "window_end": window_end, "sample_count": len(rows),
                   "metrics": metrics, "source_attribution_ids": source_ids}
        summary_hash = canonical_hash(payload)
        existing = await self._session.scalar(select(PerformanceSummaryModel).where(
            PerformanceSummaryModel.content_hash == summary_hash
        ))
        if existing is not None:
            return {"performance_summary_id": existing.performance_summary_id, **payload}
        object_id = uuid4()
        self._session.add(PerformanceSummaryModel(
            performance_summary_id=object_id, **payload,
            content_hash=summary_hash,
        ))
        return {"performance_summary_id": object_id, **payload}

    async def snapshot_recall_misses(self, threshold_version: str) -> dict:
        total = await self._session.scalar(select(func.count()).select_from(
            RecallMissEvaluationModel
        ).where(RecallMissEvaluationModel.threshold_version == threshold_version)) or 0
        misses = await self._session.scalar(select(func.count()).select_from(
            RecallMissEvaluationModel
        ).where(
            RecallMissEvaluationModel.threshold_version == threshold_version,
            RecallMissEvaluationModel.is_exceptional.is_(True),
            RecallMissEvaluationModel.was_recalled.is_(False),
        )) or 0
        evaluated_at = datetime.now(timezone.utc)
        payload = {"threshold_version": threshold_version,
                   "evaluated_at": evaluated_at, "matured_count": total,
                   "unavailable_count": 0, "evaluation_count": total,
                   "miss_count": misses,
                   "statistics": {"miss_rate": misses / total if total else None}}
        object_id = uuid4()
        self._session.add(RecallMissRunModel(
            recall_miss_run_id=object_id, **payload, content_hash=canonical_hash(payload)
        ))
        return {"recall_miss_run_id": object_id, **payload}
