"""候选生成引擎（Candidate Engine）领域契约。

整改基线：《V3_低位埋伏候选生成与排序_详细设计.md》。
本文件承载 L1 Safety Filter、扫描漏斗与 L2 多路专家召回的领域契约；
Pareto/Rank 契约在候选引擎各阶段落地时逐步扩充。

约定：
- 所有打分类契约只用于机器排序与落库展示，禁止进入 AI 输入
  （V3 域层黑名单 FORBIDDEN_UNIFIED_SCORES 语义不变）。
- missing 与 zero 严格区分：Optional 字段为 None 表示缺失，
  不得用 0 伪装；缺失信息进 data_quality / missing_fields。
- 专家评分 missing 维度计 0 分并降低 confidence（设计 §11.3），
  绝不允许 missing => 淘汰。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import Field

from app.v3.contracts.base import V3Contract
from app.v3.domain.evidence import SecurityEvidenceView
from app.v3.domain.market_data import Market, SecurityMember


class SafetyDataQuality(V3Contract):
    """单只候选的数据质量标记（设计 4.1.2 数据质量类）。"""

    quote_ok: bool = Field(description="当日核心行情可用（close>0）")
    daily_kline_ok: bool = Field(description="日K区间满足最小 bar 数要求")
    feature_stale: bool = Field(description="特征行是否已判定过期")
    adjustment_ok: bool = Field(default=True, description="复权一致性；上游无异常标记时保持 True")


class SafetyCandidateInput(V3Contract):
    """Safety Filter 的单股输入。

    由 runner 把 Universe 成员（SecurityMember）与最近已发布特征行
    （security_features）拼装而成；feature 字段缺失时传 None，
    不允许用 0 伪装。
    """

    security_id: UUID
    member: SecurityMember
    close: float | None = None
    stale: bool = False
    coverage: float | None = Field(default=None, ge=0, le=1)
    bar_count: int | None = None
    missing_fields: tuple[str, ...] = ()

    @property
    def code(self) -> str:
        return self.member.code

    @property
    def market(self) -> Market:
        return self.member.market


class SafetyVerdict(V3Contract):
    """单股 Safety 判定结果（任务书 4.3 输出契约）。

    eligible=False 时 hard_reasons 至少一条；eligible=True 时
    hard_reasons 为空。边界条件（NEW_STOCK_POOL）不算 reject，
    走独立桶，禁止静默丢弃。
    """

    security_id: UUID
    code: str
    market: Market
    eligible: bool
    hard_reasons: tuple[str, ...] = ()
    data_quality: SafetyDataQuality
    new_stock_pool: bool = False


class SafetyFilterResult(V3Contract):
    """L1 Hard Safety Filter 整体结果。

    universe_count = eligible + rejected + new_stock_pool 数量之和
    （校验器保证无静默丢弃）。
    """

    universe_count: int = Field(ge=0)
    eligible: tuple[SafetyVerdict, ...] = ()
    rejected: tuple[SafetyVerdict, ...] = ()
    new_stock_pool: tuple[SafetyVerdict, ...] = ()

    @property
    def eligible_ids(self) -> tuple[UUID, ...]:
        return tuple(item.security_id for item in self.eligible)


class StageFunnelEntry(V3Contract):
    """漏斗单层记录（设计 41 日志与可观测性）。"""

    stage: str = Field(min_length=1, max_length=32)
    input_count: int = Field(ge=0)
    output_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    error_count: int = 0
    fallback_count: int = 0
    data_stale_count: int = 0
    extras: dict[str, Any] = Field(default_factory=dict)


class ScanFunnel(V3Contract):
    """一次扫描的漏斗（L0→Final）。"""

    scan_id: UUID
    trade_date: datetime
    strategy_version: str
    parameter_version: str
    stages: tuple[StageFunnelEntry, ...] = ()

    def stage(self, name: str) -> StageFunnelEntry | None:
        return next((entry for entry in self.stages if entry.stage == name), None)


class ExpertFeatureView(V3Contract):
    """单股特征快照（专家打分输入）。

    features 为展平的特征字典：calculate_features 的 28 个主字段
    加 features JSONB 里的候选引擎扩展指标（extras）。缺失键或
    None 一律视作 missing，由专家按维度降 confidence，不得当 0。
    """

    security_id: UUID
    code: str
    close: float | None = None
    as_of: datetime | None = None
    features: dict[str, Any] = Field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    coverage: float | None = Field(default=None, ge=0, le=1)

    def number(self, key: str) -> float | None:
        """取数值特征：bool/非有限值/None 一律不算数值。"""
        value = self.features.get(key)
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value) if math.isfinite(float(value)) else None
        return None

    def flag(self, key: str) -> bool | None:
        """取布尔特征：None 视作 missing。"""
        value = self.features.get(key)
        return None if value is None else bool(value)


class ExpertInput(V3Contract):
    """专家统一输入：特征快照 + 该股证据视图（FQ/CAT 用）。"""

    feature: ExpertFeatureView
    evidence: tuple[SecurityEvidenceView, ...] = ()


class ExpertScore(V3Contract):
    """单专家对单股的评分（设计 §5-§12）。

    value 为 0~100 的专家视角分；confidence = 已生效权重/总权重
    （missing 维度不计分但拉低置信度）；reasons 为可解释依据。
    """

    value: float = Field(ge=0, le=100)
    confidence: float = Field(default=1.0, ge=0, le=1)
    reasons: tuple[str, ...] = ()
    features_used: dict[str, float | None] = Field(default_factory=dict)


class ExpertHit(V3Contract):
    """专家召回命中（任务书 §6 统一输出契约）。

    rank 为该专家内的名次（1-based，按 score 降序）。
    """

    expert: str = Field(min_length=1, max_length=32)
    security_id: UUID
    code: str
    score: float = Field(ge=0, le=100)
    rank: int = Field(ge=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    reasons: tuple[str, ...] = ()
    features: dict[str, float | None] = Field(default_factory=dict)


@runtime_checkable
class RecallExpert(Protocol):
    """召回专家协议（设计 §5.1：注入式、通道集合可配置）。"""

    def name(self) -> str:
        """专家唯一名（LP/BT/RV/AC/PB/RS/FQ/CAT）。"""
        ...

    def top_n(self) -> int:
        """该专家默认召回上限（设计 §5.2.5 等）。"""
        ...

    def required_features(self) -> tuple[str, ...]:
        """核心特征键；全部缺失时 evaluate 返回 None（不可评）。"""
        ...

    def evaluate(self, stock: ExpertInput) -> ExpertScore | None:
        """评分；None = 核心特征缺失，本专家不召回该股。"""
        ...


class UnionEntry(V3Contract):
    """Recall Union 后的单股聚合（设计 §13）。

    不设「命中最少专家数」：单路命中且排名足够高同样保留。
    """

    security_id: UUID
    code: str
    hits: tuple[ExpertHit, ...] = ()
    expert_names: tuple[str, ...] = ()
    best_score: float = Field(ge=0, le=100, description="单专家最高分")
    rrf_raw: float = Field(ge=0)
    rrf_norm: float = Field(ge=0, le=100)
    union_rank: int = Field(ge=1)


class RecallUnionResult(V3Contract):
    """Recall Union + RRF 融合结果（设计 §13-§14）。"""

    evaluated_count: int = Field(ge=0, description="送入专家的股票数")
    union_count: int = Field(ge=0, description="至少命中一路专家的股票数")
    entries: tuple[UnionEntry, ...] = ()

    @property
    def entry_ids(self) -> tuple[UUID, ...]:
        return tuple(entry.security_id for entry in self.entries)


class EnrichmentCoverage(V3Contract):
    """L3 特征补全覆盖标记（设计 §2.1 L3：周K/日K/60m/资金/基本面/催化）。

    minute_60 在 Union 阶段恒 False（60m 数据在 Machine Top120 后
    才拉取，设计 §38 成本约束）；False 是明确的缺失语义，
    不影响候选资格。
    """

    daily_kline: bool = False
    weekly_kline: bool = False
    fundamental_evidence: bool = False
    catalyst_evidence: bool = False
    minute_60: bool = False


class EnrichedCandidate(V3Contract):
    """L3 补全后的候选（UnionEntry + 完整特征/证据视图）。"""

    entry: UnionEntry
    feature: ExpertFeatureView
    evidence_count: int = Field(ge=0)
    coverage: EnrichmentCoverage


class EnrichmentResult(V3Contract):
    """L3 输出：补全候选 + 可观测异常计数。"""

    candidates: tuple[EnrichedCandidate, ...] = ()
    missing_input_count: int = Field(
        ge=0, description="Union 命中但无特征行的股票数（数据异常，不得静默）"
    )


class ParetoCandidateInput(V3Contract):
    """L4 Pareto 输入：5 个战略维度分数（设计 §16.1）。

    维度缺失传 None，比较时以中性分 50 参与并记录
    missing_dimensions（不因缺失惩罚，也不伪装成 0 分）。
    """

    security_id: UUID
    code: str
    rrf_norm: float = Field(ge=0, le=100)
    scores: dict[str, float | None] = Field(
        description="position/transition/accumulation/quality_catalyst/risk_reward",
    )
    missing_dimensions: tuple[str, ...] = ()

    def dimension(self, name: str) -> float | None:
        return self.scores.get(name)


class ParetoEntry(V3Contract):
    """Pareto 结果单股（front 编号 + 拥挤度 + 保护标记）。"""

    security_id: UUID
    code: str
    rrf_norm: float
    scores: dict[str, float | None]
    front: int = Field(
        default=0,
        ge=0,
        description="0 = 未参与分层（单专家保护并入，不经支配排序）",
    )
    crowding: float = Field(
        default=0.0,
        description="NSGA-II crowding distance；边界解用 CROWDING_BOUNDARY",
    )
    selected: bool = False
    protected: bool = False
    protected_reason: str | None = None


class ParetoResult(V3Contract):
    """L4 输出：入选池（含单专家保护）+ 全体分层信息。"""

    evaluated_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    protected_count: int = Field(ge=0)
    front_sizes: tuple[int, ...] = ()
    entries: tuple[ParetoEntry, ...] = ()

    @property
    def selected_ids(self) -> tuple[UUID, ...]:
        return tuple(
            entry.security_id for entry in self.entries
            if entry.selected or entry.protected
        )


PARETO_DIMENSIONS = (
    "position",
    "transition",
    "accumulation",
    "quality_catalyst",
    "risk_reward",
)
NEUTRAL_DIMENSION_SCORE = 50.0
CROWDING_BOUNDARY = 1.0e9  # NSGA-II 边界解拥挤度（inf 不可序列化，用大数）

MAX_PENALTY = 30.0  # 设计 §20 总处罚封顶（Safety 硬风险不在此列）


class StructureLevel(V3Contract):
    """P1-06：RR 结构候选位——每个支撑/压力必须带来源与时间戳。

    type 约定：SWING_LOW_60M / SWING_HIGH_60M（60m 结构）、
    MA20 / MA60（均线）、PULLBACK_LOW（突破回踩位）、
    LOW_60D / HIGH_60D / HIGH_120D（区间高低点回退代理）。
    swing 价格不在特征行时不得伪造候选（missing ≠ 0 语义）。
    """

    price: float = Field(gt=0)
    type: str
    as_of: datetime | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)


class RiskRewardAssessment(V3Contract):
    """L4/L5 风险回报评估（设计 §17）。

    invalidation 用最近有效 swing low（v1 以 60 日低点为代理），
    禁止固定百分比止损；RR 极高（>4）反而降档，防支撑识别过近。
    P1-06：显式注入结构候选（levels）时 invalidation 优先结构低点，
    机械 60 日最低点只作回退代理；levels 记录本次评估实际用到的候选。
    """

    support: float | None = Field(default=None, description="支撑位（60日低点代理）")
    resistance: float | None = Field(default=None, description="压力位（60日高点代理）")
    invalidation: float | None = None
    target1: float | None = None
    downside: float | None = Field(default=None, ge=0)
    upside: float | None = Field(default=None, ge=0)
    rr: float | None = Field(default=None, ge=0)
    score: float = Field(default=0.0, ge=0, le=100)
    confidence: float = Field(default=1.0, ge=0, le=1)
    levels: tuple[StructureLevel, ...] = Field(
        default=(), description="本次评估实际采用的结构候选（P1-06）",
    )


class PenaltyAssessment(V3Contract):
    """Penalty Engine 结果（设计 §20）：total <= 0，封顶 -30。"""

    total: float = Field(le=0, ge=-MAX_PENALTY)
    items: tuple[tuple[str, float, str], ...] = Field(
        default=(), description="[(规则名, 扣分, 说明)]",
    )


class SoftOpportunityResult(V3Contract):
    """L5 SoftOpportunity 8 维合成（设计 §19.1）+ Penalty 后净分。"""

    value: float = Field(ge=0, le=100)
    net_value: float = Field(ge=0, le=100, description="value + penalty（封顶后）")
    confidence: float = Field(ge=0, le=1)
    reasons: tuple[str, ...] = ()
    penalty: PenaltyAssessment
    features_used: dict[str, float | None] = Field(default_factory=dict)


class MachineRankEntry(V3Contract):
    """L5 Machine Rank 输出（设计 §21：0.35 RRF + 0.50 SoftOpp + 0.15 Pareto）。"""

    security_id: UUID
    code: str
    machine_score: float = Field(ge=0, le=100)
    components: dict[str, float] = Field(default_factory=dict)
    rank: int = Field(ge=1)
    selected: bool = False


class MachineRankResult(V3Contract):
    evaluated_count: int = Field(ge=0)
    top_n: int = Field(ge=0)
    entries: tuple[MachineRankEntry, ...] = ()


class Minute60Fact(V3Contract):
    """P1-01：Machine Top120 抓取的 60m 执行结构（抓取时点事实）。

    stale=True 或 quality=UNTRUSTED → 不得当作事实（任务书 §19.6）。
    """

    state: str = Field(description="UP/SIDEWAYS/DOWN/UNKNOWN")
    support: float | None = None
    resistance: float | None = None
    bar_count: int = Field(default=0, ge=0)
    stale: bool = False
    quality: str | None = None
    known_at: datetime | None = None


class DeepContext(V3Contract):
    """P1-01/02/03：Machine 之后、Deep 之前注入的异步数据上下文。

    minute_60_by_id 只对 Machine selected 的 security_id 提供事实；
    market_regime_score 按 feature_run_id PIT 绑定（全池共享）；
    行业上下文本轮无可靠源 → industry_missing_reason 恒
    NO_RELIABLE_INDUSTRY_CONTEXT。
    """

    minute_60_by_id: dict[UUID, Minute60Fact] = Field(default_factory=dict)
    market_regime_score: float | None = Field(default=None, ge=0, le=100)
    market_regime_source: str | None = None
    industry_missing_reason: str | None = Field(
        default="NO_RELIABLE_INDUSTRY_CONTEXT",
    )
    # R2.1-P0-01：60m 抓取 Gate 观测（available/missing/stale/error + reason 计数）
    m60_stats: dict = Field(default_factory=dict)


class DeepRankEntry(V3Contract):
    """L6 Deep Rank 输出（设计 §22.3 权重，60m/市场/行业 missing 降权）。"""

    security_id: UUID
    code: str
    deep_score: float = Field(ge=0, le=100)
    confidence: float = Field(default=1.0, ge=0, le=1)
    trend_conflict: bool = Field(
        default=False, description="周K下降+日K上升 且无明确反转证据",
    )
    components: dict[str, float | None] = Field(default_factory=dict)
    # P1-04：每组件 {raw, normalized, weight, missing, source}，
    # Why Not 展开可重算 DeepScore 而非只见最终分
    components_detail: dict[str, dict] = Field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    rank: int = Field(ge=1)
    # R2.1-P0-06：全池（≤Top120）都保留 rank/score，仅 rank<=top_n 入选
    selected: bool = Field(default=True, description="rank<=top_n 才进 Final 候选池")


class DeepRankResult(V3Contract):
    evaluated_count: int = Field(ge=0)
    top_n: int = Field(ge=0)
    entries: tuple[DeepRankEntry, ...] = ()


# ---------------------------------------------------------------------
# Step 13：全链 Trace（设计 原则E / §35.2 / §37.4）
# 任何淘汰都必须可解释、可追溯；每只股票在每个阶段留一行状态。
# ---------------------------------------------------------------------

TRACE_STAGES = (
    "UNIVERSE",
    "SAFETY",
    "RECALL",
    "PARETO",
    "MACHINE",
    "DEEP",
    "FINAL",
)

# P0-09：业务阶段序（字符串字典序 DEEP<FINAL<MACHINE≠业务序），
# TraceView 重建等一切「阶段先后」判断统一走这张表。
STAGE_ORDER = {stage: index for index, stage in enumerate(TRACE_STAGES)}


class CandidateStageRecord(V3Contract):
    """单股在单阶段的状态（candidate_snapshot 行语义）。"""

    stage: str
    alive: bool
    score: float | None = None
    rank: int | None = None
    drop_reason: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class CandidateTrace(V3Contract):
    """单股完整生命轨迹（§37.4 Why Not 数据源）。"""

    security_id: UUID
    code: str
    records: tuple[CandidateStageRecord, ...] = ()

    @property
    def last_alive_stage(self) -> str | None:
        alive = [record.stage for record in self.records if record.alive]
        return alive[-1] if alive else None

    @property
    def drop_stage(self) -> str | None:
        dead = [record for record in self.records if not record.alive]
        return dead[0].stage if dead else None

    @property
    def drop_reason(self) -> str | None:
        dead = [record for record in self.records if not record.alive]
        return dead[0].drop_reason if dead else None

    def stage(self, name: str) -> CandidateStageRecord | None:
        return next((record for record in self.records if record.stage == name), None)


class ScanTraceResult(V3Contract):
    """一次扫描的全量 trace。"""

    scan_id: UUID
    trade_date: datetime
    traces: tuple[CandidateTrace, ...] = ()

    def for_code(self, code: str) -> CandidateTrace | None:
        return next((trace for trace in self.traces if trace.code == code), None)


class CandidatePipelineResult(V3Contract):
    """候选生成主链路整体输出（Safety→…→Deep + Final + 漏斗 + Trace）。"""

    scan_id: UUID
    trade_date: datetime
    strategy_version: str
    parameter_version: str
    funnel: ScanFunnel
    trace: ScanTraceResult
    safety: SafetyFilterResult
    union: RecallUnionResult
    enrichment: EnrichmentResult
    pareto: ParetoResult
    machine: MachineRankResult
    deep: DeepRankResult
    final_entries: tuple[DeepRankEntry, ...] = Field(
        description="RAW_TOP30（§24/§25）；AI Review 接入后叠加 ai_rank/decision",
    )


# ---------------------------------------------------------------------
# Step 17-18：结果标签 / 回测指标 / 漏选审计 / 影子池（§26-§32）
# ---------------------------------------------------------------------

GOOD_LABELS = ("A", "B")  # GOOD_OPPORTUNITY = A or B（§26.2）
MATURED_LABELS = ("A", "B", "C", "NONE")  # label 非 NULL 即成熟（R2.1-P0-03）


def status_from_label(label: str | None) -> str:
    """DB 行 → status：label NULL=PENDING；A/B/C/NONE=MATURED。"""
    return "MATURED" if label in MATURED_LABELS else "PENDING"
# P1-05：AI Review 未接真实模型——Final 恒 RAW_TOP30，不得宣称已复核
AI_REVIEW_STATUS = "NOT_CONNECTED"
SHADOW_GROUPS = ("near_miss", "single_expert", "random", "other")  # §31.2


class OutcomeLabelResult(V3Contract):
    """单股单次扫描的结果标签（§26.1-§26.2）。

    R2.1-P0-03：status 与 label 语义分离——
    PENDING（未来不足 20 根）→ label=None；MATURED → label=A/B/C/NONE
    （成熟负样本显式 NONE，不再与未成熟混淆）。
    """

    code: str
    security_id: UUID | None = None
    close_t: float | None = Field(default=None, description="T 日收盘（扫描日记）")
    mfe_5: float | None = None
    mfe_10: float | None = None
    mfe_20: float | None = None
    mae_5: float | None = None
    mae_10: float | None = None
    mae_20: float | None = None
    time_to_8: int | None = Field(default=None, description="首次 +8% 的交易日序（None=未达）")
    time_to_10: int | None = None
    time_to_15: int | None = None
    status: str = Field(
        default="PENDING",
        description="PENDING（未来不足20根）/ MATURED（已成熟，label 必非 None）",
    )
    label: str | None = Field(
        default=None,
        description="A/B/C/NONE；None 仅在 status=PENDING 时允许（NONE=成熟负样本）",
    )
    bars_used: int = Field(default=0, ge=0, description="可用未来交易日数（<20 时 status 恒 PENDING）")

    @property
    def is_good(self) -> bool:
        return self.label in GOOD_LABELS


class MetricsEntry(V3Contract):
    """单条指标（Recall/Precision/NDCG）。

    P0-12：value=None + status=PENDING/NOT_APPLICABLE 显式区分
    「未成熟」与「池不足 K」，不返回伪装成真实差的 0。
    """

    metric: str
    k: int
    value: float | None = Field(default=None, ge=0, le=1)
    numerator: int = Field(ge=0, description="命中 GOOD 数")
    denominator: int = Field(ge=0, description="分母（全部 GOOD 或 K）")
    pool: str | None = Field(default=None, description="Recall 池名（Precision/NDCG 为 None）")
    ranking_source: str | None = Field(
        default=None, description="Precision/NDCG 的排名来源（machine/deep/final）",
    )
    status: str = Field(
        default="OK", description="OK / PENDING / NOT_APPLICABLE",
    )
    reason: str | None = Field(
        default=None, description="PENDING=OUTCOME_WINDOW_NOT_MATURE 等",
    )


class MissAuditEntry(V3Contract):
    """漏选审计单条（§30：这只股票为什么当时没进）。"""

    code: str
    security_id: UUID | None = None
    future_label: str
    last_alive_stage: str | None = None
    drop_stage: str | None = None
    drop_reason: str | None = None
    audit: dict[str, Any] = Field(default_factory=dict)


class ShadowSampleEntry(V3Contract):
    """影子池抽样单条（§31.2 分层）。

    sample_reason：P0-10 fallback reallocation 时记录
    quota_reallocation_from_<group>；正常抽样为 None。sample_group
    保持原语义不被缺口填充篡改。
    """

    code: str
    security_id: UUID | None = None
    sample_group: str
    drop_stage: str | None = None
    drop_reason: str | None = None
    sample_reason: str | None = None


class BacktestMetricsResult(V3Contract):
    """一次扫描的 Recall/Precision/NDCG 汇总（§27-§29）。

    R2.1-P0-04：顶层三态按 matured_count 判——
    PENDING（成熟样本 0）/ PARTIAL（部分成熟）/ OK（全部成熟）；
    指标分母只允许 status=MATURED 的 outcome。
    """

    scan_id: UUID | None = None
    good_count: int = Field(ge=0, description="Universe 中 GOOD_OPPORTUNITY 总数")
    labeled_count: int = Field(ge=0, description="已出标签的股票数")
    status: str = Field(default="PENDING", description="PENDING/PARTIAL/OK")
    matured_count: int = Field(default=0, ge=0, description="status=MATURED 的 outcome 数")
    pending_count: int = Field(default=0, ge=0, description="status=PENDING 的 outcome 数")
    entries: tuple[MetricsEntry, ...] = ()
