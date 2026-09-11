"""V3 看板中文展示映射（设计指导 §24-§60，Commit C）。

原则（§24/§85）：内部代码、DB、API 继续英文；只有展示层翻译成中文。
本模块只被 Dashboard / Scan API 的 label 附加字段消费，绝不写回 DB。

兜底规则（§36）：未映射的内部代码不上屏——显示"未配置中文名称"，
原始值留在 HTML title；同时打 `dashboard_unmapped_label` 日志，
方便开发补映射。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

UNMAPPED_LABEL = "未配置中文名称"
NAME_MISSING_LABEL = "名称暂缺"  # §59：SecurityModel.name 缺失时的展示
FINAL_TOP_SIZE = 30  # §45 最终结果文案引用 Top30

# §26 阶段中文（键 = CandidateSnapshotModel.stage）
STAGE_LABELS: dict[str, str] = {
    "UNIVERSE": "全市场",
    "SAFETY": "安全筛选",
    "RECALL": "多路召回",
    "PARETO": "多目标筛选",
    "MACHINE": "机器精排",
    "DEEP": "深度复核",
    "FINAL": "最终候选",
}

# §54 每层"通过"时的本层说明（淘汰行的说明走 DROP_REASON_LABELS）
STAGE_PASS_NOTES: dict[str, str] = {
    "UNIVERSE": "进入全市场股票池",
    "SAFETY": "交易资格与数据质量正常",
    "RECALL": "通过多路召回",
    "PARETO": "多目标前沿保留",
    "MACHINE": "进入机器精排名单",
    "DEEP": "进入深度复核名单",
    "FINAL": "已进入最终候选",
}

# §27 专家中文（展示顺序 = 引擎注册序 EXPERT_NAMES，不按字母排序）
EXPERT_LABELS: dict[str, str] = {
    "LP": "低位专家",
    "BT": "筑底专家",
    "RV": "反转专家",
    "AC": "吸筹专家",
    "PB": "回踩确认专家",
    "RS": "相对强度专家",
    "FQ": "基本面质量专家",
    "CAT": "催化事件专家",
}
EXPERT_DISPLAY_ORDER: tuple[str, ...] = ("LP", "BT", "RV", "AC", "PB", "RS", "FQ", "CAT")

# §28-§30 市场状态事实（按键取值分节——observed 在宽度/成交两节含义不同）
REGIME_SECTION_LABELS: dict[str, str] = {
    "breadth": "市场宽度",
    "turnover": "成交与流动性",
    "risk_appetite": "风险偏好事实",
}
REGIME_FACT_LABELS: dict[str, dict[str, str]] = {
    "breadth": {
        "observed": "纳入统计股票数",
        "advancing": "上涨家数",
        "declining": "下跌家数",
        "unchanged": "平盘家数",
        "mean_return_3d": "全市场近3日平均涨跌幅",
        "advance_decline_ratio": "上涨/下跌家数比",
    },
    "turnover": {
        "coverage": "成交数据覆盖率",
        "observed": "有效成交股票数",
        "total_amount": "全市场成交额",
    },
    "risk_appetite": {
        "stale_count": "陈旧数据股票数",
        "breakout_20d_count": "20日突破股票数",
        "volume_expansion_count": "放量股票数",
    },
}

# Job ID → 中文（§33）
JOB_LABELS: dict[str, str] = {
    "market-data": "行情数据准备",
    "index-benchmarks": "指数基准更新",
    "features": "全市场特征计算",
    "evidence-increment": "公告与证据增量",
    "full-recall": "全市场多路召回",
    "candidate-scan": "低位埋伏候选扫描",
    "corporate-action-match": "公司行动匹配",
    "projection-verify": "持仓投影校验",
    "performance-mature": "绩效结果成熟",
    "recall-observation-mature": "召回观察结果成熟",
    "shadow-observation": "影子策略观察",
    "expected-run-registry": "计划任务登记",
    "candidate-outcome-mature": "候选结果成熟",
}

# §49-§53 淘汰原因中文（key = drop_reason 原始值）
DROP_REASON_LABELS: dict[str, str] = {
    # Safety
    "DELISTED": "已退市",
    "DELISTING_RISK": "存在退市风险",
    "SUSPENDED": "当前停牌",
    "ST": "ST / *ST 风险股票",
    "QUOTE_INVALID": "当前行情价格无效",
    "DATA_STALE": "行情或特征数据过期",
    "DAILY_KLINE_MISSING": "缺少日K数据",
    "DAILY_KLINE_INSUFFICIENT": "日K历史长度不足",
    # Recall / Rank
    "no_expert_hit": "未命中任何召回专家",
    "deep_rank_below_top60": "已进入机器精排，但深度复核排名未进入前60",
    "final_rank_below_top30": "已通过深度复核，但最终排名未进入前30",
    # 60m 事实
    "minute_60_stale_not_fact": "60分钟数据已陈旧，本轮不作为有效事实",
    "minute_60_untrusted_quality": "60分钟数据质量不可信，本轮未采用",
    "not_fetched": "本轮未抓取60分钟数据",
    "missing": "缺少所需60分钟数据",
    # 趋势
    "weekly_down_daily_bounce_without_reversal": "周线仍处于弱势，当前更像日线反弹，反转证据不足",
    "trend_conflict_no_reversal_evidence": "多周期趋势存在冲突，尚缺少足够反转证据",
    "weekly_severe_decline": "周线下跌趋势仍较强",
    # 专家 / 结构证据
    "no_chase_extension": "短期涨幅过大，追高保护拦截",
    "trendline_break": "趋势线已跌破",
}

# §52：support_not_broken 在引擎里是加分证据而非淘汰理由——
# 展示为正向说明，绝不机械写成"淘汰原因"。
SUPPORT_NOTE_LABEL = "关键支撑仍未跌破（多头证据）"

# 状态徽章（dashboard pipeline/scan/attention 通用）
STATUS_LABELS: dict[str, str] = {
    "SUCCEEDED": "成功",
    "COMPLETED": "成功",
    "ALREADY_SUCCEEDED": "此前已成功",
    "FAILED": "失败",
    "CRITICAL": "严重失败",
    "SKIPPED": "跳过",
    "PARTIAL": "部分完成",
    "WARNING": "告警",
    "OPEN": "开启中",
    "RUNNING": "运行中",
    "LOCKED": "被锁定",
    "ACKED": "已确认",
}

# §32 交易时段
SESSION_LABELS: dict[str, str] = {
    "OPEN": "交易中",
    "PRE_OPEN": "开盘前",
    "LUNCH_BREAK": "午间休市",
    "CLOSED": "已收盘 / 非交易时段",
}
LIVE_STATUS_FIELD_LABELS: dict[str, str] = {
    "known_at": "判断时间",
    "local_time": "上海时间",
    "trade_date": "交易日期",
    "is_trading_day": "是否交易日",
    "session": "当前交易时段",
}

# §34 Attention 事件类型（AttentionEventType 全集；未知 → 其他事件）
EVENT_TYPE_LABELS: dict[str, str] = {
    "ENTRY_TRIGGER_NEAR": "接近触发买入条件",
    "ENTRY_TRIGGER_MET": "触发买入条件",
    "ENTRY_CANCEL_MET": "触发买入失效条件",
    "STOP_NEAR": "接近止损位",
    "STOP_HIT": "触及止损位",
    "TARGET_NEAR": "接近目标位",
    "TARGET_HIT": "触及目标位",
    "STRUCTURE_CHANGED": "结构变化",
    "NEW_EVIDENCE": "新证据",
    "RELATIVE_STRENGTH_CHANGED": "相对强度变化",
    "TIME_EFFICIENCY_CHANGED": "时间效率变化",
    "INTRADAY_ANOMALY": "盘中异动",
    "DATA_QUALITY_DEGRADED": "数据质量下降",
}
EVENT_TYPE_FALLBACK_LABEL = "其他事件"

# §35 特征字段中文（missing_fields / 表头共用）
FIELD_LABELS: dict[str, str] = {
    "close": "收盘价",
    "amount": "成交额",
    "coverage": "数据覆盖率",
    "return_3d": "3日涨跌幅",
    "return_5d": "5日涨跌幅",
    "return_10d": "10日涨跌幅",
    "return_20d": "20日涨跌幅",
    "return_60d": "60日涨跌幅",
    "return_120d": "120日涨跌幅",
    "return_250d": "250日涨跌幅",
    "position_60d": "60日价格位置",
    "position_120d": "120日价格位置",
    "position_250d": "250日价格位置",
    "volume_ratio_5d": "5日量比",
    "atr_pct": "ATR波动率",
    "relative_index_strength": "相对大盘强度",
    "relative_industry_strength": "相对行业强度",
    "breakout_20d": "20日突破",
    "volume_expansion": "放量",
    "pullback_20d": "20日回撤",
    "distance_60d_high": "距60日高点",
    "distance_60d_low": "距60日低点",
    "market_regime_score": "市场状态分",
    "weekly_state": "周线状态",
    "daily_state": "日线状态",
    "minute_60_state": "60分钟状态",
}

# Deep 60m 事实质量（candidate_engine §19.6：UNTRUSTED 不得当作事实）
QUALITY_LABELS: dict[str, str] = {
    "TRUSTED": "数据可信",
    "UNTRUSTED": "数据不可信",
}

# §60 市场代码（内部查询仍 SH/SZ/BJ）
MARKET_LABELS: dict[str, str] = {
    "SH": "沪市",
    "SZ": "深市",
    "BJ": "北交所",
}

# §80 错误摘要的用户可读归类（原始 exception 进 <details>）
ERROR_KIND_LABELS: dict[str, str] = {
    "timeout": "运行超时",
    "provider": "数据源异常",
    "database": "数据库异常",
    "generic": "任务执行失败",
}


def zh_label(mapping: Mapping[str, str], raw: Any) -> str:
    """§36 兜底：未映射内部代码不上屏——返回"未配置中文名称"并打
    `dashboard_unmapped_label` 日志；None/空串统一为占位符"—"。"""
    if raw is None or str(raw) == "":
        return "—"
    key = str(raw)
    label = mapping.get(key)
    if label is not None:
        return label
    logger.warning(
        "dashboard_unmapped_label mapping=%s raw=%s",
        type(mapping).__name__, key,
    )
    return UNMAPPED_LABEL


def zh(raw: Any, mapping: Mapping[str, str], fallback: str = UNMAPPED_LABEL) -> str:
    """带自定义兜底文案的映射（如 event_type 未知 → 其他事件）。"""
    if raw is None or str(raw) == "":
        return "—"
    return mapping.get(str(raw), fallback)


def event_type_label(raw: Any) -> str:
    return zh(raw, EVENT_TYPE_LABELS, EVENT_TYPE_FALLBACK_LABEL)
