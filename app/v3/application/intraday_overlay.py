"""RT-03：Intraday Overlay + Lightweight Scanner（实时方案 §4.1 L1 / §5 / §27）。

- Overlay：最近一次 Published EOD Feature + 实时 Quote 轻量叠加，
  绝不全市场重算 250 日特征；特征/Levels 缺失的字段诚实 None；
- Scanner：全市场只用轻指标（量比/涨跌/相对指数/Overlay 标记）产出
  IntradayAttentionCandidate —— 是"值得让 AI 重新看"的事实，不是买入名单；
  stale Quote 绝不入选；
- Active Pool：EOD 候选 + Watchlist + Portfolio + 盘中异常去重合并。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from app.v3.domain.intraday import (
    ActivePoolEntry,
    IntradayAttentionCandidate,
    IntradayOverlayFeature,
    IntradayQuoteSnapshot,
)

# 轻指标初始阈值（实时方案 §22：配置化，不硬编码进业务语义）
VOLUME_SURGE_RATIO = 2.0
STRONG_VS_INDEX = 0.03
SHARP_DROP = -0.05
NEAR_LEVEL_PCT = 0.005
PULLBACK_MA_PCT = 0.005


def _ratio(latest: float, base: float | None) -> float | None:
    if base is None or base <= 0:
        return None
    return latest / base - 1


class IntradayOverlayService:
    """L1 全市场 Overlay：EOD 特征 × 实时 Quote，纯函数、可批量。"""

    def build(
        self,
        *,
        code: str,
        market: str,
        quote: IntradayQuoteSnapshot,
        feature: Any | None,
        levels: dict[str, float] | None = None,
        index_return: float | None = None,
        as_of: datetime | None = None,
    ) -> IntradayOverlayFeature:
        as_of = as_of or quote.as_of
        latest = quote.last_price
        prev_close = quote.prev_close
        intraday_return = _ratio(latest, prev_close) if latest is not None else None
        range_pct = (
            (quote.high - quote.low) / prev_close
            if None not in (quote.high, quote.low, prev_close) and prev_close > 0
            else None
        )
        feats = (feature.features or {}) if feature is not None else {}
        ma20 = feats.get("ma20")
        vs_ma = {
            f"vs_ma{period}": _ratio(latest, feats.get(f"ma{period}"))
            for period in (5, 10, 20, 60)
        } if latest is not None else {
            f"vs_ma{period}": None for period in (5, 10, 20, 60)
        }
        prev_high = (levels or {}).get("prev_high_20d")
        prev_low = (levels or {}).get("prev_low_20d")
        support = (levels or {}).get("support")
        resistance = (levels or {}).get("resistance")
        breakout_now = (
            latest > prev_high if latest is not None and prev_high is not None else None
        )
        touched_above = (
            quote.high is not None and prev_high is not None
            and quote.high > prev_high
        )
        failed_breakout = (
            bool(touched_above and latest is not None and latest <= prev_high)
            if prev_high is not None else None
        )
        # "在位附近"：水平位两侧 NEAR_LEVEL_PCT 内都算（突破瞬间同样贴位）
        near_resistance = (
            abs(resistance - latest) / latest <= NEAR_LEVEL_PCT
            if latest is not None and resistance is not None and latest > 0
            else None
        )
        near_support = (
            abs(latest - support) / latest <= NEAR_LEVEL_PCT
            if latest is not None and support is not None and latest > 0
            else None
        )
        return IntradayOverlayFeature(
            code=code, market=market, as_of=as_of,
            known_at=quote.known_at, source=quote.source,
            latest_price=latest, prev_close=prev_close,
            intraday_return=intraday_return,
            intraday_range_pct=range_pct,
            intraday_volume_ratio=quote.volume_ratio,
            intraday_turnover=quote.turnover_rate,
            vs_ma5=vs_ma["vs_ma5"], vs_ma10=vs_ma["vs_ma10"],
            vs_ma20=vs_ma["vs_ma20"], vs_ma60=vs_ma["vs_ma60"],
            vs_prev_high_20d=_ratio(latest, prev_high),
            vs_prev_low_20d=_ratio(latest, prev_low),
            breakout_now=breakout_now,
            pullback_now=(
                bool(
                    intraday_return is not None and intraday_return < 0
                    and ma20 is not None and latest is not None
                    and abs(latest / ma20 - 1) <= PULLBACK_MA_PCT
                )
                if ma20 is not None else None
            ),
            failed_breakout=failed_breakout,
            near_support=near_support,
            near_resistance=near_resistance,
            relative_index_return=(
                intraday_return - index_return
                if intraday_return is not None and index_return is not None
                else None
            ),
            stale=quote.stale,
            feature_as_of=getattr(feature, "as_of", None),
            feature_available=feature is not None,
            ma_available=ma20 is not None,
        )


class IntradayScannerService:
    """全市场轻量异常扫描（§5.2）：输出 Attention 候选，不是买入名单。"""

    def __init__(
        self,
        *,
        volume_surge_ratio: float = VOLUME_SURGE_RATIO,
        strong_vs_index: float = STRONG_VS_INDEX,
        sharp_drop: float = SHARP_DROP,
    ) -> None:
        self._volume_surge = volume_surge_ratio
        self._strong_vs_index = strong_vs_index
        self._sharp_drop = sharp_drop

    def scan(
        self,
        quotes: Iterable[IntradayQuoteSnapshot],
        overlays: dict[str, IntradayOverlayFeature] | None = None,
        *,
        index_return: float | None = None,
        as_of: datetime,
    ) -> tuple[IntradayAttentionCandidate, ...]:
        overlays = overlays or {}
        candidates: list[IntradayAttentionCandidate] = []
        for quote in quotes:
            if quote.stale:
                # 数据质量优先：stale Quote 绝不进扫描结果
                continue
            reasons: list[str] = []
            intraday_return = _ratio(quote.last_price, quote.prev_close)
            if quote.volume_ratio is not None and quote.volume_ratio >= self._volume_surge:
                reasons.append("VOLUME_SURGE")
            if (
                intraday_return is not None and index_return is not None
                and intraday_return - index_return >= self._strong_vs_index
            ):
                reasons.append("STRONG_VS_INDEX")
            if intraday_return is not None and intraday_return <= self._sharp_drop:
                reasons.append("SHARP_DROP")
            overlay = overlays.get(quote.code)
            if overlay is not None:
                if overlay.breakout_now:
                    reasons.append("BREAKOUT_20D")
                if overlay.failed_breakout:
                    reasons.append("FAILED_BREAKOUT")
                if overlay.near_support:
                    reasons.append("NEAR_SUPPORT")
                if overlay.near_resistance:
                    reasons.append("NEAR_RESISTANCE")
            if not reasons:
                continue
            candidates.append(IntradayAttentionCandidate(
                code=quote.code, market=quote.market,
                as_of=as_of, known_at=quote.known_at,
                source=quote.source, reasons=tuple(reasons),
                latest_price=quote.last_price,
                intraday_return=intraday_return,
                volume_ratio=quote.volume_ratio,
            ))
        return tuple(candidates)


class ActiveIntradayUniverseService:
    """§5.3 Active Pool：四路来源去重合并，保留来源溯源。"""

    @staticmethod
    def merge(
        *,
        eod_candidates: Iterable[tuple[str, str]],
        watchlist: Iterable[tuple[str, str]],
        portfolio: Iterable[tuple[str, str]],
        intraday_attention: Iterable[tuple[str, str]],
    ) -> tuple[ActivePoolEntry, ...]:
        merged: dict[tuple[str, str], ActivePoolEntry] = {}
        for source, keys in (
            ("EOD_CANDIDATE", eod_candidates),
            ("WATCHLIST", watchlist),
            ("PORTFOLIO", portfolio),
            ("INTRADAY_ATTENTION", intraday_attention),
        ):
            for market, code in keys:
                key = (market, code)
                if key in merged:
                    merged[key].sources.append(source)
                else:
                    merged[key] = ActivePoolEntry(
                        market=market, code=code, sources=[source],
                    )
        return tuple(merged.values())
