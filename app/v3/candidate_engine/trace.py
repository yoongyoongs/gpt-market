"""Step 13：全链 Trace 采集（设计 原则E / §35.2）。

每阶段记录每只股票 (stage, alive, score, rank, drop_reason)；
已淘汰股票在后续阶段继续记行（alive=False，沿用首次淘汰原因），
保证「生命轨迹」完整可查（§37.4 Why Not）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.v3.domain.candidate_engine import (
    CandidateStageRecord,
    CandidateTrace,
    DeepRankResult,
    MachineRankResult,
    ParetoResult,
    RecallUnionResult,
    SafetyFilterResult,
    ScanTraceResult,
    TRACE_STAGES,
)

__all__ = ["ScanTraceBuilder"]


class ScanTraceBuilder:
    """按阶段顺序喂数据，build() 产出全量 trace。

    用内部可变 dict 承载状态，build 时一次性构建冻结契约
    （V3Contract frozen，与 Pareto _Ranked 同一模式）。
    """

    def __init__(self, scan_id: UUID, trade_date: datetime) -> None:
        self._scan_id = scan_id
        self._trade_date = trade_date
        self._codes: dict[UUID, str] = {}
        self._records: dict[UUID, dict[str, CandidateStageRecord]] = {}

    # ---- 各阶段 ----

    def record_universe(self, candidates: list) -> None:
        """candidates: SafetyCandidateInput 序列；全体存活。"""
        for candidate in candidates:
            self._codes[candidate.security_id] = candidate.code
            self._records[candidate.security_id] = {
                "UNIVERSE": CandidateStageRecord(stage="UNIVERSE", alive=True),
            }

    def record_safety(self, result: SafetyFilterResult) -> None:
        for verdict in result.eligible:
            self._alive(verdict.security_id, "SAFETY", detail={
                "data_quality": verdict.data_quality.model_dump(),
            })
        for verdict in result.rejected:
            self._dead(
                verdict.security_id, "SAFETY",
                reason="+".join(verdict.hard_reasons) or "SAFETY_REJECT",
            )
        for verdict in result.new_stock_pool:
            # 新股不算淘汰，也不进主链：独立桶语义
            self._dead(verdict.security_id, "SAFETY", reason="NEW_STOCK_POOL")

    def record_recall(
        self,
        union: RecallUnionResult,
        eligible_ids: tuple[UUID, ...] | list[UUID],
    ) -> None:
        by_id = {entry.security_id: entry for entry in union.entries}
        for security_id in eligible_ids:
            entry = by_id.get(security_id)
            if entry is None:
                self._dead(security_id, "RECALL", reason="no_expert_hit")
            else:
                self._alive(
                    security_id, "RECALL",
                    score=entry.best_score, rank=entry.union_rank,
                    detail={"experts": list(entry.expert_names), "rrf_norm": entry.rrf_norm},
                )

    def record_pareto(self, result: ParetoResult) -> None:
        by_id = {entry.security_id: entry for entry in result.entries}
        for security_id, entry in by_id.items():
            if entry.selected or entry.protected:
                self._alive(security_id, "PARETO", score=entry.rrf_norm, detail={
                    "front": entry.front,
                    "crowding": entry.crowding,
                    "protected": entry.protected,
                    "protected_reason": entry.protected_reason,
                })
            else:
                self._dead(
                    security_id, "PARETO",
                    reason=f"pareto_front{entry.front}_not_selected",
                    # P0-08：未入选也保存 front/crowding/rrf，Why Not 可查
                    detail={
                        "front": entry.front,
                        "crowding": entry.crowding,
                        "rrf_norm": entry.rrf_norm,
                    },
                )

    def record_machine(self, result: MachineRankResult) -> None:
        for entry in result.entries:
            if entry.selected:
                self._alive(entry.security_id, "MACHINE", score=entry.machine_score, rank=entry.rank)
            else:
                # P0-08：未入 Top120 也保存真实 rank/score（§37.4 Why Not
                # 必须能查到 Machine #167）
                self._dead(
                    entry.security_id, "MACHINE",
                    reason=f"machine_rank_below_top{result.top_n}",
                    score=entry.machine_score,
                    rank=entry.rank,
                )

    def record_deep(self, result: DeepRankResult) -> None:
        for entry in result.entries:
            self._alive(entry.security_id, "DEEP", score=entry.deep_score, rank=entry.rank)
        # 未进 Deep 的股票（Machine 全量 entries 里未入选者）由
        # record_machine 已标 dead；Machine 只保留 top_n 标记，其余
        # entries 也已带 alive=False 行，这里无需补记。

    def record_final(self, entries: tuple) -> None:
        """RAW_TOP30：Deep 前 30（AI Review 接入后在此叠加 ai_rank）。"""
        final_ids = {entry.security_id for entry in entries}
        for entry in entries:
            self._alive(entry.security_id, "FINAL", score=entry.deep_score, rank=entry.rank)
        # Deep Top60 但未进 Final 的补 dead 行（P0-08：保留 deep rank/score）
        for security_id, records in self._records.items():
            deep_record = records.get("DEEP")
            if deep_record is not None and deep_record.alive and security_id not in final_ids:
                self._dead(
                    security_id, "FINAL",
                    reason="final_not_top30",
                    score=deep_record.score,
                    rank=deep_record.rank,
                )

    # ---- 输出 ----

    def build(self) -> ScanTraceResult:
        traces: list[CandidateTrace] = []
        for security_id, stage_records in self._records.items():
            ordered = tuple(
                stage_records[stage] for stage in TRACE_STAGES if stage in stage_records
            )
            traces.append(CandidateTrace(
                security_id=security_id,
                code=self._codes.get(security_id, ""),
                records=ordered,
            ))
        traces.sort(key=lambda trace: trace.code)
        return ScanTraceResult(
            scan_id=self._scan_id,
            trade_date=self._trade_date,
            traces=tuple(traces),
        )

    # ---- 内部 ----

    def _alive(
        self, security_id: UUID, stage: str, *, score: float | None = None,
        rank: int | None = None, detail: dict | None = None,
    ) -> None:
        self._records.setdefault(security_id, {})[stage] = CandidateStageRecord(
            stage=stage, alive=True, score=score, rank=rank, detail=detail or {},
        )

    def _dead(
        self, security_id: UUID, stage: str, *, reason: str,
        score: float | None = None, rank: int | None = None,
        detail: dict | None = None,
    ) -> None:
        """已淘汰股票继续记行；未经过前面阶段的（保护并入等）先补 UNIVERSE。

        P0-08：淘汰行同样保存 score/rank/detail——「未入选 ≠ 未评分」，
        Why Not 查询（Machine #121~）必须能拿到真实排名。"""
        records = self._records.setdefault(security_id, {})
        records.setdefault("UNIVERSE", CandidateStageRecord(stage="UNIVERSE", alive=True))
        records[stage] = CandidateStageRecord(
            stage=stage, alive=False, score=score, rank=rank,
            drop_reason=reason, detail=detail or {},
        )
