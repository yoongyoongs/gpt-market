"""Risk / Reward 评估（设计 §17）。

v1 数据面：swing low/high 价格不在特征行里，用 60 日区间高低点
代理支撑/压力（distance_60d_low/high 反推），invalidation=支撑、
target1=压力；禁固定百分比止损（§17.3）。RR 评分用设计 §17.5
锚点平滑插值；RR>4 降档回 95 防支撑识别过近的虚假高分。
"""

from __future__ import annotations

from app.v3.candidate_engine.soft import is_finite
from app.v3.domain.candidate_engine import ExpertFeatureView, RiskRewardAssessment

__all__ = ["RiskRewardService", "risk_reward_score"]

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


class RiskRewardService:
    """RR = upside / max(downside, eps)，v1 以 60 日区间为结构代理。"""

    def evaluate(self, view: ExpertFeatureView) -> RiskRewardAssessment:
        f = view.number
        close = view.close
        dist_low = f("distance_60d_low")
        dist_high = f("distance_60d_high")
        if close is None or not is_finite(close) or close <= 0:
            return RiskRewardAssessment(score=0.0, confidence=0.0)
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
