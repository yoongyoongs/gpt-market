"""RT-01：Intraday 实时行情 Domain Contracts（实时方案 §4.1/§17.1）。

L0 Quote 快照与分钟/日/周 Bar 的 V3 契约。所有时点必须 aware：
- event_time：行情发生时点（上游报价时点）；
- fetch_time：系统抓取时点；
- known_at：系统已知时点（= fetch_time，审计对齐）。
未收盘 K 线 bar_status=PROVISIONAL，绝不冒充正式历史。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import model_validator

from app.v3.contracts.base import V3Contract, require_aware

BarStatus = Literal["PROVISIONAL", "CLOSED"]


def _require_aware_fields(values: dict, fields: tuple[str, ...]) -> dict:
    for field in fields:
        value = values.get(field)
        if isinstance(value, datetime):
            require_aware(value, field)
    return values


class IntradayQuoteSnapshot(V3Contract):
    """L0 全市场实时 Quote 快照（§4.1 字段清单）。"""

    code: str
    market: str
    name: str | None = None
    last_price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    change: float | None = None
    change_pct: float | None = None
    volume: int | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    bid: float | None = None
    ask: float | None = None
    suspended: bool = False

    event_time: datetime
    fetch_time: datetime
    known_at: datetime
    as_of: datetime
    source: str
    upstream_source: str
    quality: str
    stale: bool
    # R4-P0-001：未来事实（known_at/event_time > as_of）绝不冒充新鲜价，
    # 降级为 stale 并显式给出原因
    stale_reason: str | None = None
    confidence: str = "MEDIUM"

    @model_validator(mode="before")
    @classmethod
    def _check_aware(cls, values):
        return _require_aware_fields(
            values, ("event_time", "fetch_time", "known_at", "as_of")
        )


class IntradayBar(V3Contract):
    bar_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    bar_status: BarStatus = "CLOSED"


class IntradayBarSeries(V3Contract):
    """单周期分钟/日/周 Bar 序列（§4.2 provisional/closed 语义）。"""

    period: str
    status: Literal["AVAILABLE", "UNKNOWN"]
    reason: str | None = None
    bars: tuple[IntradayBar, ...] = ()
    bar_count: int = 0
    provisional: bool = False
    stale: bool | None = None
    # 分钟事实是抓取时点事实：精度一律显式 LIMITED，绝不伪装精确历史
    precision: Literal["LIMITED", "UNKNOWN"] = "LIMITED"
    first_bar_time: datetime | None = None
    last_bar_time: datetime | None = None
    # R4-P1-006：每周期 provenance——消费方能看到"60m 实际 Eastmoney /
    # 15m 实际 Tencent fallback / week 实际 aggregate:day:tencent"。
    # source = 数据取得路径（KlineResult.source，如 aggregate:day:tencent）；
    # upstream_source = 时戳来源（timestamp_source：eastmoney/tencent/fetch_time）。
    source: str | None = None
    upstream_source: str | None = None
    known_at: datetime | None = None
    quality: str | None = None
    confidence: str | None = None
    fallback_used: bool = False


class IntradayBarsResult(V3Contract):
    code: str
    as_of: datetime
    known_at: datetime
    source: str
    periods: dict[str, IntradayBarSeries] = {}

    @model_validator(mode="before")
    @classmethod
    def _check_aware(cls, values):
        return _require_aware_fields(values, ("as_of", "known_at"))


class PeriodStructure(V3Contract):
    """单周期结构事实（RT-02 / §4.4）：趋势 + 支撑/压力，确定性推导。"""

    trend: Literal["UP", "DOWN", "SIDEWAYS", "UNKNOWN"]
    support: float | None = None
    resistance: float | None = None
    # 周/日未收盘 K 线显式 PROVISIONAL；UNKNOWN 周期无此字段
    bar_status: BarStatus | None = None
    bar_count: int = 0
    reason: str | None = None
    stale: bool | None = None


class IntradayStructureSnapshot(V3Contract):
    """多周期结构快照（§4.4）：周/日/60/15/5 + 反转状态 + 周日冲突显式表达。

    reversal_state 只是确定性事实标记（POSSIBLE=下降趋势中的反弹候选），
    CONFIRMED 必须由可解释证据（AI/Evidence）给出，服务器绝不自行确认。
    """

    code: str
    as_of: datetime
    known_at: datetime
    source: str
    latest_price: float | None = None
    weekly: PeriodStructure
    daily: PeriodStructure
    reversal_state: Literal["NONE", "POSSIBLE", "UNKNOWN"] = "UNKNOWN"
    conflict: str | None = None
    conflict_rule: str | None = None
    periods: dict[str, PeriodStructure] = {}
    stale: bool = False

    @model_validator(mode="before")
    @classmethod
    def _check_aware(cls, values):
        return _require_aware_fields(values, ("as_of", "known_at"))


class IntradayOverlayFeature(V3Contract):
    """L1 全市场 Intraday Overlay（§4.1）：EOD 特征 × 实时 Quote 轻量叠加。

    特征或 Levels 缺失的派生字段一律 None（诚实），绝不编造。
    """

    code: str
    market: str
    as_of: datetime
    known_at: datetime
    source: str
    latest_price: float | None = None
    prev_close: float | None = None
    intraday_return: float | None = None
    intraday_range_pct: float | None = None
    intraday_volume_ratio: float | None = None
    intraday_turnover: float | None = None
    vs_ma5: float | None = None
    vs_ma10: float | None = None
    vs_ma20: float | None = None
    vs_ma60: float | None = None
    vs_prev_high_20d: float | None = None
    vs_prev_low_20d: float | None = None
    breakout_now: bool | None = None
    pullback_now: bool | None = None
    failed_breakout: bool | None = None
    near_support: bool | None = None
    near_resistance: bool | None = None
    relative_index_return: float | None = None
    stale: bool = False
    feature_as_of: datetime | None = None
    feature_available: bool = False
    ma_available: bool = False

    @model_validator(mode="before")
    @classmethod
    def _check_aware(cls, values):
        return _require_aware_fields(values, ("as_of", "known_at"))


class IntradayAttentionCandidate(V3Contract):
    """§5.2 盘中轻量异常扫描输出：只是事实与原因，不是买入名单。"""

    code: str
    market: str
    as_of: datetime
    known_at: datetime
    source: str
    reasons: tuple[str, ...]
    latest_price: float | None = None
    intraday_return: float | None = None
    volume_ratio: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _check_aware(cls, values):
        return _require_aware_fields(values, ("as_of", "known_at"))


class ActivePoolEntry(V3Contract):
    """§5.3 Active Intraday Universe 成员：代码 + 来源溯源。"""

    market: str
    code: str
    sources: list[str] = []
