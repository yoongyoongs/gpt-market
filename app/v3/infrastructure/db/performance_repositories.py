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
    AIResultEnvelopeModel,
    BarSeriesRevisionModel,
    ContextPackModel,
    DecisionModel,
    EntryPlanModel,
    EvidenceRecordModel,
    PerformanceAttributionModel,
    PerformanceSummaryModel,
    RecallMissEvaluationModel,
    RecallMissRunModel,
    RegressionCaseExecutionModel,
    RegressionCaseModel,
    ReplayRunModel,
    SecurityFeatureModel,
    SecurityModel,
    TradeLedgerModel,
)
from app.v3.repositories.errors import RepositoryConflictError, RepositoryNotFoundError

# RC-06B 特征核验只比较仅依赖 pinned 日 K 的列（storage 精度容差内）
_RUN_FEATURE_FIELDS = (
    "close", "return_3d", "return_5d", "return_10d", "return_20d",
    "return_60d", "return_120d", "return_250d",
    "position_60d", "position_120d", "position_250d",
    "ma5", "ma10", "ma20", "ma60", "ma20_slope", "ma60_slope",
    "atr14", "atr_pct", "volatility20",
    "distance_60d_high", "distance_60d_low",
    "breakout_20d", "pullback_20d", "amount",
    "volume_ratio_5d", "volume_expansion",
)


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
                "entry_plan_id": row.entry_plan_id,
                "entry_plan_version": row.entry_plan_version,
            }
            for row in rows
        ]

    async def attribution_id_exists(self, attribution_id: UUID) -> bool:
        """按确定性 attribution_id 兜底判重：known_at 变化会改变内容哈希，
        但 uuid5 主键不变 —— 重跑必须以主键存在性为准（幂等 skip）。"""
        return await self._session.get(
            PerformanceAttributionModel, attribution_id
        ) is not None

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

    async def regime_replay_input(self, feature_run_id: UUID) -> dict | None:
        """PF-002：Regime 重算回放输入 —— immutable Regime 快照 + 该 run
        全部特征行（重算只用落库事实，不重取外部数据）。"""
        regime = await self._session.scalar(
            select(MarketRegimeSnapshotModel).where(
                MarketRegimeSnapshotModel.feature_run_id == feature_run_id
            )
        )
        if regime is None:
            return None
        rows = (
            await self._session.scalars(
                select(SecurityFeatureModel).where(
                    SecurityFeatureModel.feature_run_id == feature_run_id
                )
            )
        ).all()
        return {
            "regime": {
                "breadth": dict(regime.breadth or {}),
                "turnover": dict(regime.turnover or {}),
                "risk_appetite_facts": dict(regime.risk_appetite_facts or {}),
                "stale": regime.stale,
                "stale_reason": dict(regime.stale_reason or {}),
            },
            "features": [
                {
                    "stale": row.stale,
                    "return_3d": None if row.return_3d is None else float(row.return_3d),
                    "amount": None if row.amount is None else float(row.amount),
                    "volume_expansion": row.volume_expansion,
                    "breakout_20d": row.breakout_20d,
                }
                for row in rows
            ],
        }

    async def context_pack_replay_payloads(self, context_pack_ids) -> list[dict]:
        """PF-002：Context Pack 证据选择重放输入（immutable payload + 查询键）。"""
        payloads = []
        for pack_id in context_pack_ids:
            pack = await self._session.get(ContextPackModel, pack_id)
            if pack is None:
                payloads.append({
                    "context_pack_id": pack_id, "available": False,
                    "reason": "MISSING_CONTEXT_PACK",
                })
                continue
            payloads.append({
                "context_pack_id": pack_id, "available": True,
                "payload": dict(pack.payload or {}),
                "as_of": pack.as_of,
                "subject_type": pack.subject_type,
                "subject_id": pack.subject_id,
                "context_level": pack.context_level,
                "token_budget": pack.token_budget,
            })
        return payloads

    async def replay_gate(
        self, bar_revision_ids, evidence_ids, context_pack_ids, *, replay_as_of: datetime,
    ) -> tuple[list[dict], dict]:
        """RC-06B 第一道 Gate（保留 `_check_references` 语义）。"""
        checks: list[dict] = []
        revision_set: dict[str, list] = {"bars": [], "evidence": [], "contexts": []}
        await self._check_references(
            bar_revision_ids, BarSeriesRevisionModel, "revision_id", "bars",
            replay_as_of, checks, revision_set,
        )
        await self._check_references(
            evidence_ids, EvidenceRecordModel, "evidence_id", "evidence",
            replay_as_of, checks, revision_set,
        )
        await self._check_references(
            context_pack_ids, ContextPackModel, "context_pack_id", "contexts",
            replay_as_of, checks, revision_set,
        )
        return checks, revision_set

    async def replay_verification_targets(self, context_pack_ids) -> list[dict]:
        """RC-06B：pinned Context Pack 的特征核验目标解析。"""
        targets = []
        for pack_id in context_pack_ids:
            pack = await self._session.get(ContextPackModel, pack_id)
            if pack is None:
                targets.append({"context_pack_id": pack_id, "available": False,
                                "reason": "MISSING_CONTEXT_PACK"})
                continue
            if pack.subject_type != "SECURITY":
                targets.append({"context_pack_id": pack_id, "available": False,
                                "reason": "NON_SECURITY_SUBJECT_NOT_VERIFIED"})
                continue
            try:
                market, code = str(pack.subject_id).split(":", 1)
            except ValueError:
                targets.append({"context_pack_id": pack_id, "available": False,
                                "reason": "UNPARSEABLE_SECURITY_SUBJECT"})
                continue
            security_id = await self._session.scalar(
                select(SecurityModel.security_id).where(
                    SecurityModel.market == market, SecurityModel.code == code,
                )
            )
            if security_id is None:
                targets.append({"context_pack_id": pack_id, "available": False,
                                "reason": "SECURITY_NOT_FOUND"})
                continue
            targets.append({
                "context_pack_id": pack_id, "available": True,
                "feature_run_id": pack.feature_run_id, "security_id": security_id,
            })
        return targets

    async def load_run_feature(
        self, feature_run_id: UUID, security_id: UUID,
    ) -> dict | None:
        """RC-06B：读取当时落库的 immutable Feature（可比较列）。"""
        model = await self._session.scalar(
            select(SecurityFeatureModel).where(
                SecurityFeatureModel.feature_run_id == feature_run_id,
                SecurityFeatureModel.security_id == security_id,
            )
        )
        if model is None:
            return None
        stored = {field: getattr(model, field) for field in _RUN_FEATURE_FIELDS}
        stored = {
            key: (float(value) if value is not None else None)
            for key, value in stored.items()
        }
        features = dict(model.features or {})
        stored["daily_trend_state"] = features.get("daily_trend_state")
        return stored

    async def immutable_ai_result_for_pack(self, context_pack_id: UUID) -> dict | None:
        """RC-06B：当时 immutable 的 AI Result（结果回放来源），无模型重跑。"""
        model = (
            await self._session.scalars(
                select(AIResultEnvelopeModel)
                .where(AIResultEnvelopeModel.context_pack_id == context_pack_id)
                .order_by(AIResultEnvelopeModel.produced_at.desc())
                .limit(1)
            )
        ).first()
        if model is None:
            return None
        return {
            "result_id": model.result_id,
            "result_type": model.result_type,
            "provider": model.provider,
            "model": model.model,
            "content_hash": model.content_hash,
            "payload": dict(model.payload or {}),
        }

    async def record_replay(self, command: ReplayRunCreate, payload: dict) -> dict:
        self._session.add(ReplayRunModel(
            replay_run_id=command.replay_run_id,
            content_hash=canonical_hash(payload),
            **{key: payload[key] for key in (
                "strategy_version", "replay_as_of", "revision_set",
                "parameters", "status", "leakage_checks", "result",
            )},
        ))
        return {"replay_run_id": command.replay_run_id, **payload}

    async def get_regression_case(self, case_id: UUID) -> dict | None:
        model = await self._session.get(RegressionCaseModel, case_id)
        if model is None:
            return None
        return {
            "regression_case_id": model.regression_case_id,
            "name": model.name,
            "strategy_version": model.strategy_version,
            "replay_as_of": model.replay_as_of,
            "input_requirements": dict(model.input_requirements or {}),
            "expected_invariants": dict(model.expected_invariants or {}),
            "source_replay_run_id": model.source_replay_run_id,
            "status": model.status,
            "blocked_reason": model.blocked_reason,
        }

    async def record_regression_execution(self, payload: dict) -> dict:
        """RC-06C：真执行结果 append-only 落库（PASS/FAIL/BLOCKED + diff）。"""
        self._session.add(RegressionCaseExecutionModel(
            execution_id=payload["execution_id"],
            regression_case_id=payload["regression_case_id"],
            status=payload["status"],
            replay_run_id=payload["replay_run_id"],
            blocked_reason=payload["blocked_reason"],
            invariant_results=payload["invariant_results"],
            diff=payload["diff"],
            known_at=payload["known_at"],
            content_hash=canonical_hash(payload),
        ))
        return payload

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
