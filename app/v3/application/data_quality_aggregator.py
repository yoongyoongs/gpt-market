"""F6-04/P1-09：唯一 DataQualityAggregator（§3 冻结规则）。

输入组件状态（quote/feature coverage、四源 status、index quality、deep
结果），统一输出 GOOD / DEGRADED / UNAVAILABLE。Worker / HTTP / MCP 只
透传该结果——禁止任一下游把 DEGRADED 恢复成 AVAILABLE。

冻结规则（按最严重生效）：

- UNAVAILABLE：quote QUOTE_FAILED / UNAVAILABLE_FOR_FULL_MARKET_SCAN /
  expected=0（无 expected 无法宣称任何覆盖）；
- DEGRADED：quote PARTIAL；任一来源（features/eod/watchlist/portfolio）
  FAILED；feature overlay 覆盖率 < 0.9 或为 0；index 不可用/不可信；
  Deep 有请求但存在 UNKNOWN 周期或整票失败；
- 其余 → GOOD。
"""

from __future__ import annotations

from typing import Any

GOOD = "GOOD"
DEGRADED = "DEGRADED"
UNAVAILABLE = "UNAVAILABLE"

_QUOTE_UNAVAILABLE = {"QUOTE_FAILED", "UNAVAILABLE_FOR_FULL_MARKET_SCAN"}
_SOURCE_NAMES = ("features", "eod", "watchlist", "portfolio")


def aggregate_data_quality(
    *,
    quote_status: str | None = None,
    quote_expected: int | None = None,
    quote_coverage: float | None = None,
    feature_status: str | None = None,
    feature_coverage: float | None = None,
    feature_actual: int | None = None,
    source_statuses: dict[str, dict[str, Any]] | None = None,
    index_status: str | None = None,
    deep_requested: bool = False,
    deep_statuses: tuple[str, ...] = (),
) -> str:
    """聚合数据质量。纯函数，无 IO，便于冻结验收。"""
    result = GOOD

    def _worse(level: str) -> None:
        nonlocal result
        if level == UNAVAILABLE:
            result = UNAVAILABLE
        elif level == DEGRADED and result != UNAVAILABLE:
            result = DEGRADED

    # 1) quote 覆盖（Coverage Gate 语义，F6-05：availability 与
    #    full_market_complete 分离，这里只管可用性）
    if quote_status is not None and quote_status in _QUOTE_UNAVAILABLE:
        _worse(UNAVAILABLE)
    elif quote_status == "PARTIAL":
        _worse(DEGRADED)
    if quote_expected == 0:
        _worse(UNAVAILABLE)

    # 2) 四数据源独立失败（R5-P1-003 隔离语义的聚合面）
    sources = source_statuses or {}
    for name in _SOURCE_NAMES:
        status = (sources.get(name) or {}).get("status")
        if status == "FAILED":
            _worse(DEGRADED)

    # 3) feature overlay 覆盖（P1-09 Case E：0% Feature 不得显示健康）
    if feature_status == "FAILED":
        _worse(DEGRADED)
    if feature_coverage is not None and feature_coverage < 0.9:
        _worse(DEGRADED)
    if feature_coverage is None and feature_actual == 0:
        _worse(DEGRADED)

    # 4) index 质量（F6-08：stale/untrusted 指数只废 STRONG_VS_INDEX，
    #    但质量面必须如实降级）
    if index_status is not None and index_status != "AVAILABLE":
        _worse(DEGRADED)

    # 5) deep：有请求时，UNKNOWN/失败的存在即降级
    if deep_requested and deep_statuses:
        if any(status != "AVAILABLE" for status in deep_statuses):
            _worse(DEGRADED)

    return result
