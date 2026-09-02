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
