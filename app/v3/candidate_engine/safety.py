"""L1 Hard Safety Filter（任务书 §4 / 设计 §4）。

整改核心：把旧 V1/V2 扫描器中"机会型指标单条件硬过滤"
（主板白名单/涨幅/量比/成交额/MA20 状态等）从新链路剔除，
L1 只保留交易资格与数据质量类事实判断：

允许（设计 §4.1）：
- 退市/退市风险（DELISTED / DELISTING_RISK）
- 停牌（SUSPENDED，trading_status 或 suspended 标记任一命中）
- ST / *ST（exclude_st 配置化，默认排除）
- 核心行情缺失/价格异常（close 缺失或 <= close_floor）
- 日K不足（bar_count < min_daily_bars；新股走 NEW_STOCK_POOL 不算 reject）
- 特征行整体缺失（DAILY_KLINE_MISSING，事实不可用）
- 特征过期（DATA_STALE，reject_stale 配置化）

禁止出现（转 Soft 评分，见 experts/ 与 scoring.py）：
price<MA20、MA20 斜率、MACD、RSI、相对强弱、成交额、换手率、
量比、近期涨幅、周K趋势、行业强弱、52w 位置高低。

复权异常与多源冲突：当前上游 security_features 无独立异常标记
（quality/source_errors 语义未约定），第一版不伪造检测，
adjustment_ok 恒 True 并在实施报告"未完成项"中说明。

本服务为纯函数式：输入 SafetyCandidateInput 序列，输出
SafetyFilterResult，无隐式全局状态，全部规则可单测。
"""

from __future__ import annotations

from app.v3.candidate_engine.config import SafetyConfig
from app.v3.domain.candidate_engine import (
    SafetyCandidateInput,
    SafetyDataQuality,
    SafetyFilterResult,
    SafetyVerdict,
)

# universe provider 目前只产出 ACTIVE/SUSPENDED（universe.py:54,262,285,307）。
# DELISTED 为防御未来值域扩展预留；UNKNOWN 视为数据缺失而非资格问题。
_TRADING_STATUS_ACTIVE = "ACTIVE"
_TRADING_STATUS_SUSPENDED = "SUSPENDED"
_TRADING_STATUS_DELISTED = "DELISTED"


class HardSafetyFilterService:
    """逐股资格判定；每条硬规则独立触发，多原因并存便于追溯。"""

    def __init__(self, config: SafetyConfig) -> None:
        self._config = config

    def execute(
        self, candidates: tuple[SafetyCandidateInput, ...]
    ) -> SafetyFilterResult:
        eligible: list[SafetyVerdict] = []
        rejected: list[SafetyVerdict] = []
        new_stock_pool: list[SafetyVerdict] = []

        for candidate in candidates:
            verdict = self._judge(candidate)
            if verdict.new_stock_pool:
                # 新股历史不足：独立桶，不计 reject（设计 §4.1.1 禁止静默丢弃）
                new_stock_pool.append(verdict)
            elif not verdict.eligible:
                rejected.append(verdict)
            else:
                eligible.append(verdict)

        universe_count = len(candidates)
        return SafetyFilterResult(
            universe_count=universe_count,
            eligible=tuple(eligible),
            rejected=tuple(rejected),
            new_stock_pool=tuple(new_stock_pool),
        )

    def _judge(self, candidate: SafetyCandidateInput) -> SafetyVerdict:
        cfg = self._config
        member = candidate.member
        reasons: list[str] = []

        # ---- 交易资格类 ----
        if member.trading_status == _TRADING_STATUS_DELISTED:
            reasons.append("DELISTED")
        if member.delisting_risk:
            reasons.append("DELISTING_RISK")
        if member.suspended or member.trading_status == _TRADING_STATUS_SUSPENDED:
            reasons.append("SUSPENDED")
        if member.is_st and cfg.exclude_st:
            reasons.append("ST")

        # ---- 数据质量类 ----
        quote_ok = candidate.close is not None and candidate.close > cfg.close_floor
        if not quote_ok:
            reasons.append("QUOTE_INVALID")
        if candidate.stale and cfg.reject_stale:
            reasons.append("DATA_STALE")

        if candidate.bar_count is None:
            # 无特征行：事实不可用（连 close 都无法交叉验证的场景）
            reasons.append("DAILY_KLINE_MISSING")
            daily_kline_ok = False
        else:
            daily_kline_ok = candidate.bar_count >= cfg.min_daily_bars
            if not daily_kline_ok:
                reasons.append("DAILY_KLINE_INSUFFICIENT")

        quality = SafetyDataQuality(
            quote_ok=quote_ok,
            daily_kline_ok=daily_kline_ok,
            feature_stale=candidate.stale,
            adjustment_ok=True,
        )

        eligible = not reasons
        # 新股池：资格与行情均正常，仅历史不足。is_new_listing 由
        # universe 成员标记；bar 不足但资格正常的新股进池不进 reject。
        new_stock = (
            eligible is False
            and set(reasons) <= {"DAILY_KLINE_INSUFFICIENT"}
            and member.is_new_listing
        )
        return SafetyVerdict(
            security_id=candidate.security_id,
            code=member.code,
            market=member.market,
            eligible=eligible,
            hard_reasons=tuple(reasons),
            data_quality=quality,
            new_stock_pool=new_stock,
        )
