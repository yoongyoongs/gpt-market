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
