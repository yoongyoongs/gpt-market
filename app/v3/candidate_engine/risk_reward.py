"""Risk / Reward 评估（设计 §17）。

v1 数据面：swing low/high 价格不在特征行里，用 60 日区间高低点
代理支撑/压力（distance_60d_low/high 反推），invalidation=支撑、
target1=压力；禁固定百分比止损（§17.3）。RR 评分用设计 §17.5
锚点平滑插值；RR>4 降档回 95 防支撑识别过近的虚假高分。

P1-06（任务书 §24，可选升级）：显式注入结构候选（StructureLevel）
时，invalidation 优先结构低点（SWING_LOW_60M > 均线 > 60 日低点回退），
不机械取 60 日最低点；每个候选必须带 source(type) 与时间戳。
未注入 levels 时保持 v1 路径不变（默认零破坏）。
"""

from __future__ import annotations

from datetime import datetime

from app.v3.candidate_engine.soft import is_finite
from app.v3.domain.candidate_engine import (
    ExpertFeatureView,
    RiskRewardAssessment,
    StructureLevel,
)

__all__ = [
    "RiskRewardService",
    "risk_reward_score",
    "structure_levels_from_features",
    "structure_levels_from_minute60",
]

# 设计 §17.5 评分锚点：(RR, score)
_RR_ANCHORS: tuple[tuple[float, float], ...] = (
    (0.0, 10.0),
    (0.8, 30.0),
    (1.2, 55.0),
    (1.8, 80.0),
    (2.5, 100.0),
    (4.0, 95.0),
)
_DOWNSIDE_FLOOR = 0.01  # 支撑贴现价 <1% 时 RR 失真，分数按下限保护

# P1-06：invalidation 结构优先序（小者优先）；机械区间低点只作回退。
# 结构低点（60m swing/回踩）优先于均线，均线优先于机械 60 日低点。
_SUPPORT_TYPE_PRIORITY = {
    "SWING_LOW_60M": 0,
    "PULLBACK_LOW": 0,
    "MA20": 1,
    "MA60": 1,
    "LOW_60D": 2,
}
# 压力侧：60m swing high 优先，区间前高就近即可（前高无"结构确认"概念）
_RESISTANCE_TYPE_PRIORITY = {
    "SWING_HIGH_60M": 0,
    "HIGH_120D": 1,
    "HIGH_60D": 1,
}
# 来源基线置信：60m 结构位 > 均线 > 机械区间极值
_LEVEL_CONFIDENCE = {
    "SWING_LOW_60M": 0.8,
    "SWING_HIGH_60M": 0.8,
    "PULLBACK_LOW": 0.7,
    "MA20": 0.6,
    "MA60": 0.6,
    "LOW_60D": 0.5,
    "HIGH_60D": 0.5,
    "HIGH_120D": 0.5,
}


def risk_reward_score(rr: float) -> float:
    """按 §17.5 锚点分段平滑插值（连续、可微、无阶梯跳变）。"""
    if not is_finite(rr):
        return 0.0
    if rr <= _RR_ANCHORS[0][0]:
        return _RR_ANCHORS[0][1]
    for (x0, y0), (x1, y1) in zip(_RR_ANCHORS, _RR_ANCHORS[1:]):
        if rr <= x1:
            t = (rr - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t
    return _RR_ANCHORS[-1][1]


def _level_from_distance(
    view: ExpertFeatureView, key: str, level_type: str
) -> StructureLevel | None:
    """distance_* 特征反推价位：price = close / (1 + dist)。

    dist 约定：close 相对极值的价格距离（close=10、60d low=9.09 →
    dist_low≈0.10；60d high=12.5 → dist_high=-0.20）。
    """
    close = view.close
    if close is None or not is_finite(close) or close <= 0:
        return None
    dist = view.number(key)
    if dist is None or not is_finite(dist) or 1.0 + dist <= 0:
        return None
    price = close / (1.0 + dist)
    if price <= 0 or not is_finite(price):
        return None
    return StructureLevel(
        price=round(price, 6), type=level_type,
        as_of=view.as_of, confidence=_LEVEL_CONFIDENCE[level_type],
    )


def structure_levels_from_features(
    view: ExpertFeatureView,
) -> tuple[StructureLevel, ...]:
    """P1-06：从特征行反推可用结构候选。

    可得：MA20（close_ma20_distance）、LOW_60D / HIGH_60D /
    HIGH_120D。swing low/high 价格不在特征行 → 不伪造候选；
    60m 结构由 structure_levels_from_minute60 从抓取事实构建。
    """
    levels: list[StructureLevel] = []
    for key, level_type in (
        ("close_ma20_distance", "MA20"),
        ("distance_60d_low", "LOW_60D"),
        ("distance_60d_high", "HIGH_60D"),
        ("distance_120d_high", "HIGH_120D"),
    ):
        level = _level_from_distance(view, key, level_type)
        if level is not None:
            levels.append(level)
    return tuple(levels)


def structure_levels_from_minute60(
    fact: dict | None, *, as_of: datetime | None = None
) -> tuple[StructureLevel, ...]:
    """P1-06：60m 抓取事实（P1-01 Minute60Fact）→ 结构候选。

    stale / 未抓取 / 无价位 → 空（不当事实，§19.6 语义一致）。
    """
    if fact is None or fact.get("stale"):
        return ()
    levels: list[StructureLevel] = []
    for key, level_type in (("support", "SWING_LOW_60M"), ("resistance", "SWING_HIGH_60M")):
        price = fact.get(key)
        if price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price <= 0 or not is_finite(price):
            continue
        levels.append(StructureLevel(
            price=round(price, 6), type=level_type,
            as_of=as_of or fact.get("known_at"),
            confidence=_LEVEL_CONFIDENCE[level_type],
        ))
    return tuple(levels)


def _pick(
    candidates: list[StructureLevel], priorities: dict[str, int], *, nearest_low: bool
) -> StructureLevel | None:
    """选位：先按类型优先级（小者优先），同级取离 close 最近
    （支撑侧取 price 最大，压力侧取 price 最小）。未知类型按最低优先。"""
    if not candidates:
        return None
    top_priority = min(priorities.get(level.type, 2) for level in candidates)
    same = [level for level in candidates if priorities.get(level.type, 2) == top_priority]
    if nearest_low:
        return max(same, key=lambda level: level.price)
    return min(same, key=lambda level: level.price)


class RiskRewardService:
    """RR = upside / max(downside, eps)；v1 以 60 日区间为结构代理，
    P1-06 注入结构候选时优先结构低点。"""

    def evaluate(
        self, view: ExpertFeatureView, levels: tuple[StructureLevel, ...] | None = None
    ) -> RiskRewardAssessment:
        close = view.close
        if close is None or not is_finite(close) or close <= 0:
            return RiskRewardAssessment(score=0.0, confidence=0.0)
        if levels is None:
            return self._evaluate_proxy(view, close)
        return self._evaluate_structured(view, close, levels)

    # ---------- P1-06：结构候选路径 ----------

    def _evaluate_structured(
        self, view: ExpertFeatureView, close: float,
        levels: tuple[StructureLevel, ...],
    ) -> RiskRewardAssessment:
        valid = [
            level for level in levels
            if is_finite(level.price) and 0 < level.price != close
        ]
        below = [level for level in valid if level.price < close]
        above = [level for level in valid if level.price > close]
        invalidation = _pick(below, _SUPPORT_TYPE_PRIORITY, nearest_low=True)
        target1 = _pick(above, _RESISTANCE_TYPE_PRIORITY, nearest_low=False)
        if invalidation is None or target1 is None:
            # 结构候选不足以定价支撑或压力 → 回退 v1 代理路径（不空转）
            return self._evaluate_proxy(view, close)
        support = invalidation.price
        resistance = target1.price
        downside = (close - support) / close
        upside = resistance / close - 1.0
        rr = upside / max(downside, _DOWNSIDE_FLOOR) if upside > 0 else 0.0
        score = risk_reward_score(rr)
        # 评估置信 = 两侧所选候选来源置信的较小者（来源弱 → 评估弱）
        confidence = min(invalidation.confidence, target1.confidence)
        if downside < _DOWNSIDE_FLOOR:
            confidence = min(confidence, 0.5)
        return RiskRewardAssessment(
            support=support,
            resistance=resistance,
            invalidation=support,
            target1=resistance,
            downside=round(downside, 6),
            upside=round(upside, 6),
            rr=round(rr, 6),
            score=round(score, 4),
            confidence=round(confidence, 4),
            levels=(invalidation, target1),
        )

    # ---------- v1：60 日区间代理路径（默认，零破坏） ----------

    def _evaluate_proxy(self, view: ExpertFeatureView, close: float) -> RiskRewardAssessment:
        f = view.number
        dist_low = f("distance_60d_low")
        dist_high = f("distance_60d_high")
        if not is_finite(dist_low) or not is_finite(dist_high):
            return RiskRewardAssessment(score=0.0, confidence=0.0)
        assert dist_low is not None and dist_high is not None

        support = close / (1.0 + dist_low) if 1.0 + dist_low > 0 else None
        resistance = close / (1.0 + dist_high) if 1.0 + dist_high > 0 else None
        invalidation = support
        target1 = resistance
        downside = dist_low / (1.0 + dist_low)  # (close-support)/close
        # P0-06：upside = resistance/close - 1 = -dist_high/(1+dist_high)。
        # 旧式 -dist_high 把贴高收益系统性放大（10/12 → 16.67% 而非 20%）。
        upside = (
            -dist_high / (1.0 + dist_high)
            if dist_high < 0 and 1.0 + dist_high > 0
            else 0.0  # 贴高（close 即 60d 高点）时上方无空间
        )
        rr = upside / max(downside, _DOWNSIDE_FLOOR) if upside > 0 else 0.0
        score = risk_reward_score(rr)
        # 防虚假高分：支撑过近（downside < 1%）时 RR 无意义，压到下限档
        confidence = 0.5 if downside < _DOWNSIDE_FLOOR else 1.0
        return RiskRewardAssessment(
            support=support,
            resistance=resistance,
            invalidation=invalidation,
            target1=target1,
            downside=round(downside, 6),
            upside=round(upside, 6),
            rr=round(rr, 6),
            score=round(score, 4),
            confidence=confidence,
        )
