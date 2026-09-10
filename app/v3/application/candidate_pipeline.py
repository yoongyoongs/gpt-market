"""Step 13：候选生成主链路装配（Safety→专家→Union→Pareto→Machine→Deep）。

纯装配：不实现新算法，只按设计 §2.1 串联既有服务、计时漏斗、
采集全链 Trace。Final=RAW_TOP30（Deep 前 30，§24/§25），AI Review
接入后在此叠加 ai_rank/decision（本文件不改）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.v3.candidate_engine.deep_rank import DeepRankService
from app.v3.candidate_engine.enrichment import FeatureEnrichmentService
from app.v3.candidate_engine.experts import default_experts
from app.v3.candidate_engine.machine_rank import MachineRankService
from app.v3.candidate_engine.pareto import ParetoService
from app.v3.candidate_engine.penalty import PenaltyEngine
from app.v3.candidate_engine.risk_reward import (
    RiskRewardService,
    merge_structure_levels,
    structure_levels_from_features,
    structure_levels_from_minute60,
)
from app.v3.candidate_engine.safety import HardSafetyFilterService
from app.v3.candidate_engine.soft import weighted_combine
from app.v3.candidate_engine.soft_opportunity import SoftOpportunityService
from app.v3.candidate_engine.trace import ScanTraceBuilder
from app.v3.candidate_engine.union import RecallUnionService
from app.v3.candidate_engine.config import SafetyConfig
from app.v3.domain.candidate_engine import (
    AI_REVIEW_STATUS,
    CandidatePipelineResult,
    DeepContext,
    ExpertHit,
    ExpertInput,
    MachineRankResult,
    ParetoCandidateInput,
    ParetoResult,
    SafetyCandidateInput,
    SafetyFilterResult,
    ScanFunnel,
    ScanTraceResult,
    StageFunnelEntry,
    TRACE_STAGES,
)

__all__ = ["CandidatePipeline", "FINAL_TOP_N", "AI_REVIEW_STATUS", "MachinePhaseState"]

FINAL_TOP_N = 30  # RAW_TOP30；行业分散化（§25）待行业数据接入


@dataclass
class MachinePhaseState:
    """P1-01 Phase A 输出：Machine Top120 为止的全部中间状态。

    Phase B（complete_deep）消费；禁止复制执行 Safety~Machine。
    """

    scan_id: UUID
    trade_date: datetime
    strategy_version: str
    parameter_version: str
    builder: ScanTraceBuilder
    stages: list[StageFunnelEntry] = field(default_factory=list)
    candidates: tuple[SafetyCandidateInput, ...] = ()
    stocks_by_id: dict = field(default_factory=dict)
    safety: SafetyFilterResult | None = None
    union: object | None = None
    enrichment: object | None = None
    pareto: ParetoResult | None = None
    machine: MachineRankResult | None = None
    rr_scores: dict = field(default_factory=dict)
    expert_scores_by_id: dict = field(default_factory=dict)


class CandidatePipeline:
    def __init__(
        self,
        *,
        safety_config: SafetyConfig | None = None,
        pareto_service: ParetoService | None = None,
        machine_service: MachineRankService | None = None,
        deep_service: DeepRankService | None = None,
        final_top_n: int = FINAL_TOP_N,
    ) -> None:
        self._safety = HardSafetyFilterService(safety_config or SafetyConfig())
        self._experts = default_experts()
        self._union = RecallUnionService()
        self._enrichment = FeatureEnrichmentService()
        self._pareto = pareto_service or ParetoService()
        self._risk_reward = RiskRewardService()
        self._soft = SoftOpportunityService(penalty_engine=PenaltyEngine())
        self._machine = machine_service or MachineRankService()
        self._deep = deep_service or DeepRankService()
        self._final_top_n = final_top_n

    def execute(
        self,
        candidates: tuple[SafetyCandidateInput, ...],
        stocks_by_id: dict,
        *,
        trade_date: datetime,
        strategy_version: str = "v3",
        parameter_version: str = "v1",
        scan_id=None,
        deep_context: DeepContext | None = None,
    ) -> CandidatePipelineResult:
        """同步全链入口 = Phase A + Phase B（不复制执行前段）。

        deep_context=None（离线/测试路径）时 60m/Regime 恒 missing 降权。
        """
        state = self.run_to_machine(
            candidates, stocks_by_id,
            trade_date=trade_date,
            strategy_version=strategy_version,
            parameter_version=parameter_version,
            scan_id=scan_id,
        )
        return self.complete_deep(state, deep_context=deep_context)

    def run_to_machine(
        self,
        candidates: tuple[SafetyCandidateInput, ...],
        stocks_by_id: dict,
        *,
        trade_date: datetime,
        strategy_version: str = "v3",
        parameter_version: str = "v1",
        scan_id=None,
    ) -> MachinePhaseState:
        """P1-01 Phase A：Safety→Recall→Pareto→Machine Top120。"""
        scan_id = scan_id or uuid4()
        builder = ScanTraceBuilder(scan_id, trade_date)
        stages: list[StageFunnelEntry] = []

        # ---- L0 Universe ----
        builder.record_universe(list(candidates))
        stages.append(self._stage("UNIVERSE", len(candidates), len(candidates)))

        # ---- L1 Safety ----
        started = time.perf_counter()
        safety = self._safety.execute(candidates)
        builder.record_safety(safety)
        eligible_ids = safety.eligible_ids
        stages.append(self._stage(
            "SAFETY", len(candidates), len(eligible_ids), started,
            extra={"rejected": len(safety.rejected), "new_stock_pool": len(safety.new_stock_pool)},
        ))

        # ---- L2 专家召回 + Union ----
        started = time.perf_counter()
        eligible_inputs = [stocks_by_id[sid] for sid in eligible_ids if sid in stocks_by_id]
        expert_results: dict[str, tuple[ExpertHit, ...]] = {}
        for expert in self._experts:
            expert_results[expert.name()] = expert.run(eligible_inputs)
        union = self._union.execute(expert_results, evaluated_count=len(eligible_inputs))
        builder.record_recall(union, eligible_ids)
        stages.append(self._stage(
            "RECALL", len(eligible_ids), union.union_count, started,
            extra={name: len(hits) for name, hits in expert_results.items()},
        ))

        # ---- L3 Enrichment ----
        started = time.perf_counter()
        enrichment = self._enrichment.execute(union, stocks_by_id)
        stages.append(self._stage(
            "ENRICH", union.union_count, len(enrichment.candidates), started,
            extra={"missing_input": enrichment.missing_input_count},
        ))

        # ---- L4 Pareto（5 维映射 §16.1-§16.2）----
        started = time.perf_counter()
        expert_scores_by_id = self._expert_scores_by_id(expert_results)
        pareto_inputs = [
            self._pareto_input(entry, stocks_by_id, expert_scores_by_id)
            for entry in union.entries
            if entry.security_id in stocks_by_id
        ]
        pareto = self._pareto.execute(pareto_inputs, expert_results)
        builder.record_pareto(pareto)
        stages.append(self._stage(
            "PARETO", union.union_count, len(pareto.selected_ids), started,
            extra={"protected": pareto.protected_count, "fronts": list(pareto.front_sizes[:5])},
        ))

        # ---- L5 SoftOpportunity + Machine Rank ----
        started = time.perf_counter()
        machine, rr_scores = self._run_machine(pareto, stocks_by_id, expert_scores_by_id)
        builder.record_machine(machine)
        stages.append(self._stage(
            "MACHINE", pareto.evaluated_count, machine.top_n, started,
        ))

        return MachinePhaseState(
            scan_id=scan_id,
            trade_date=trade_date,
            strategy_version=strategy_version,
            parameter_version=parameter_version,
            builder=builder,
            stages=stages,
            candidates=candidates,
            stocks_by_id=stocks_by_id,
            safety=safety,
            union=union,
            enrichment=enrichment,
            pareto=pareto,
            machine=machine,
            rr_scores=rr_scores,
            expert_scores_by_id=expert_scores_by_id,
        )

    def complete_deep(
        self,
        state: MachinePhaseState,
        *,
        deep_context: DeepContext | None = None,
    ) -> CandidatePipelineResult:
        """P1-01 Phase B：Deep→Final（60m/Regime/Industry 上下文注入）。"""
        deep_context = deep_context or DeepContext()

        # ---- L6 Deep Rank ----
        started = time.perf_counter()
        deep = self._run_deep(
            state.machine, state.stocks_by_id, state.rr_scores,
            state.expert_scores_by_id, deep_context,
        )
        state.builder.record_deep(deep)
        state.stages.append(self._stage("DEEP", state.machine.top_n, deep.top_n, started))

        # ---- Final (RAW_TOP30) ----
        started = time.perf_counter()
        final_entries = tuple(deep.entries[: self._final_top_n])
        state.builder.record_final(final_entries)
        state.stages.append(self._stage("FINAL", deep.top_n, len(final_entries), started))

        trace = state.builder.build()
        funnel = ScanFunnel(
            scan_id=state.scan_id,
            trade_date=state.trade_date,
            strategy_version=state.strategy_version,
            parameter_version=state.parameter_version,
            stages=tuple(state.stages),
        )
        return CandidatePipelineResult(
            scan_id=state.scan_id,
            trade_date=state.trade_date,
            strategy_version=state.strategy_version,
            parameter_version=state.parameter_version,
            funnel=funnel,
            trace=trace,
            safety=state.safety,
            union=state.union,
            enrichment=state.enrichment,
            pareto=state.pareto,
            machine=state.machine,
            deep=deep,
            final_entries=final_entries,
        )

    # ------------------------------------------------------------------

    def _run_machine(
        self,
        pareto: ParetoResult,
        stocks_by_id: dict,
        expert_scores_by_id: dict,
    ) -> tuple[MachineRankResult, dict]:
        pareto_by_id = {entry.security_id: entry for entry in pareto.entries}
        pool: list[dict] = []
        rr_scores: dict = {}
        for security_id in pareto.selected_ids:
            entry = pareto_by_id[security_id]
            stock = stocks_by_id.get(security_id)
            if stock is None:
                continue
            # R2.1-P1-01：Machine 阶段用特征行结构候选定价支撑/压力
            # （此时还没有 60m）；候选不足自动回退 v1 代理路径
            rr = self._risk_reward.evaluate(
                stock.feature,
                levels=structure_levels_from_features(stock.feature),
            )
            rr_scores[security_id] = rr.score
            soft = self._soft.evaluate(stock, expert_scores_by_id.get(security_id, {}), rr)
            pool.append({
                "security_id": security_id,
                "code": entry.code,
                "rrf_norm": entry.rrf_norm,
                "soft_net": soft.net_value,
                "front": entry.front,
                "crowding": entry.crowding,
            })
        return self._machine.execute(pool), rr_scores

    def _run_deep(
        self, machine: MachineRankResult, stocks_by_id: dict, rr_scores: dict,
        expert_scores_by_id: dict, deep_context: DeepContext | None = None,
    ) -> "DeepRankResult":
        from app.v3.domain.candidate_engine import DeepRankResult

        deep_context = deep_context or DeepContext()
        pool: list[dict] = []
        for entry in machine.entries:
            if not entry.selected:
                continue
            stock = stocks_by_id.get(entry.security_id)
            features = stock.feature.features if stock is not None else {}
            # P1-01：60m 抓取事实（仅 Machine selected 有）；缺失=未接入
            m60 = deep_context.minute_60_by_id.get(entry.security_id)
            # R2.1-P1-01：Deep 阶段 RiskRewardRefined——60m 结构位与
            # 特征行结构位合并（60m 优先），重新评估 RR；provenance
            # （type/source/as_of/confidence）随 levels 进 components_detail
            if stock is not None:
                merged_levels = merge_structure_levels(
                    structure_levels_from_minute60(
                        m60.model_dump() if m60 is not None else None
                    ),
                    structure_levels_from_features(stock.feature),
                )
                refined = self._risk_reward.evaluate(
                    stock.feature, levels=merged_levels,
                )
                rr_refined = refined.score
                rr_levels = [level.model_dump() for level in refined.levels]
            else:
                rr_refined = rr_scores.get(entry.security_id)
                rr_levels = []
            pool.append({
                "security_id": entry.security_id,
                "code": entry.code,
                "machine_score": entry.machine_score,
                "machine_rank": entry.rank,
                "weekly_state": features.get("weekly_trend_state"),
                "daily_state": features.get("daily_trend_state"),
                "multi_state": features.get("multi_timeframe_state"),
                "weekly_slope_8w": features.get("weekly_slope_8w"),
                "weekly_decline_deceleration": features.get("weekly_decline_deceleration"),
                "rr_refined_score": rr_refined,
                "rr_levels": rr_levels,
                # P0-07：反转证据条件组 B 需要 RV 专家分
                "rv_score": expert_scores_by_id.get(entry.security_id, {}).get("RV"),
                # P1-01：60m 事实注入（模型转 dict；None=未接入路径）
                "minute_60": m60.model_dump() if m60 is not None else None,
                "minute_60_state": m60.state if m60 is not None else None,
                # P1-02：feature_run_id PIT 的 regime 分（全池共享）
                "market_regime_score": deep_context.market_regime_score,
                "market_regime_source": deep_context.market_regime_source,
                # P1-03：无可靠行业源 → missing 带原因
                "industry_missing_reason": deep_context.industry_missing_reason,
            })
        return self._deep.execute(pool)

    @staticmethod
    def _expert_scores_by_id(
        expert_results: dict[str, tuple[ExpertHit, ...]],
    ) -> dict:
        scores: dict = {}
        for hits in expert_results.values():
            for hit in hits:
                scores.setdefault(hit.security_id, {})[hit.expert] = hit.score
        return scores

    def _pareto_input(
        self,
        entry,
        stocks_by_id: dict,
        expert_scores_by_id: dict,
    ) -> ParetoCandidateInput:
        scores_map = expert_scores_by_id.get(entry.security_id, {})
        stock = stocks_by_id.get(entry.security_id)
        rr_score = self._risk_reward.evaluate(stock.feature).score if stock is not None else None
        position = scores_map.get("LP")
        accumulation = scores_map.get("AC")
        bt, rv = scores_map.get("BT"), scores_map.get("RV")
        transition = (
            (bt + rv) / 2.0 if bt is not None and rv is not None
            else (bt if bt is not None else rv)
        )
        fq, cat = scores_map.get("FQ"), scores_map.get("CAT")
        # P0-05：FQ/CAT 全 missing → quality=None（真实 missing 语义）；
        # 有值但真 0 分 → 保留 0.0（不再被 `or None` 吞掉）。
        quality = weighted_combine([
            ("fundamental", 55.0, None if fq is None else fq / 100.0),
            ("catalyst", 45.0, None if cat is None else cat / 100.0),
        ])[0]
        missing = [
            name for name, value in (
                ("position", position), ("transition", transition),
                ("accumulation", accumulation), ("quality_catalyst", quality),
                ("risk_reward", rr_score),
            ) if value is None
        ]
        return ParetoCandidateInput(
            security_id=entry.security_id,
            code=entry.code,
            rrf_norm=entry.rrf_norm,
            scores={
                "position": position,
                "transition": transition,
                "accumulation": accumulation,
                "quality_catalyst": quality,
                "risk_reward": rr_score,
            },
            missing_dimensions=tuple(missing),
        )

    @staticmethod
    def _stage(name: str, input_count: int, output_count: int, started: float | None = None,
               extra: dict | None = None) -> StageFunnelEntry:
        duration_ms = int((time.perf_counter() - started) * 1000) if started else 0
        return StageFunnelEntry(
            stage=name,
            input_count=input_count,
            output_count=output_count,
            duration_ms=duration_ms,
            extras=extra or {},
        )
