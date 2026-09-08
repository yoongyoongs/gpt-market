"""候选生成引擎（Candidate Engine）领域契约。

整改基线：《V3_低位埋伏候选生成与排序_详细设计.md》。
本文件承载 L1 Safety Filter 与扫描漏斗的领域契约；Recall/Pareto/Rank
契约在候选引擎各阶段落地时逐步扩充。

约定：
- 所有打分类契约只用于机器排序与落库展示，禁止进入 AI 输入
  （V3 域层黑名单 FORBIDDEN_UNIFIED_SCORES 语义不变）。
- missing 与 zero 严格区分：Optional 字段为 None 表示缺失，
  不得用 0 伪装；缺失信息进 data_quality / missing_fields。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.v3.contracts.base import V3Contract
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
