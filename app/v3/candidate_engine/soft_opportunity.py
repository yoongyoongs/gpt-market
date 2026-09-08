"""L5 SoftOpportunity（设计 §19.1 八维权重）+ Penalty 集成（§20）。

权重：Position 20 / Transition 20 / Accumulation 15 / Bottoming 10 /
NonChase 10 / Fundamental 8 / Catalyst 7 / RiskReward 10。
missing 维 0 分 + 降 confidence；净分 = value + penalty（封顶 -30）。
"""

from __future__ import annotations

from app.v3.candidate_engine.soft import is_finite, soft_low_better, weighted_combine
from app.v3.domain.candidate_engine import (
    ExpertInput,
    PenaltyAssessment,
    RiskRewardAssessment,
    SoftOpportunityResult,
)

__all__ = ["SoftOpportunityService"]

WEIGHTS = {
    "position": 20.0,
    "transition": 20.0,
    "accumulation": 15.0,
    "bottoming": 10.0,
    "nonchase": 10.0,
    "fundamental": 8.0,
    "catalyst": 7.0,
    "risk_reward": 10.0,
}


class SoftOpportunityService:
    """专家分 → 八维机会分；Penalty 由引擎独立计算后扣减。"""

    def __init__(self, penalty_engine=None):
        self.penalty_engine = penalty_engine

    def evaluate(
        self,
        stock: ExpertInput,
        expert_scores: dict[str, float],
        risk_reward: RiskRewardAssessment,
    ) -> SoftOpportunityResult:
        """expert_scores: {LP/BT/RV/AC/FQ/CAT: 0~100}；缺专家名=missing。"""
        f = stock.feature.number
        position = expert_scores.get("LP")
        bottoming = expert_scores.get("BT")
        reversal = expert_scores.get("RV")
        accumulation = expert_scores.get("AC")
        fundamental = expert_scores.get("FQ")
        catalyst = expert_scores.get("CAT")
        transition = (
            (bottoming + reversal) / 2.0
            if bottoming is not None and reversal is not None
            else (bottoming if bottoming is not None else reversal)
        )
        value, confidence, reasons, features_used = weighted_combine([
            ("position", WEIGHTS["position"], None if position is None else position / 100.0),
            ("transition", WEIGHTS["transition"], None if transition is None else transition / 100.0),
            ("accumulation", WEIGHTS["accumulation"], None if accumulation is None else accumulation / 100.0),
            ("bottoming", WEIGHTS["bottoming"], None if bottoming is None else bottoming / 100.0),
            ("nonchase", WEIGHTS["nonchase"], self._nonchase(stock.feature)),
            ("fundamental", WEIGHTS["fundamental"], None if fundamental is None else fundamental / 100.0),
            ("catalyst", WEIGHTS["catalyst"], None if catalyst is None else catalyst / 100.0),
            ("risk_reward", WEIGHTS["risk_reward"], None if risk_reward.score <= 0 and risk_reward.confidence <= 0 else risk_reward.score / 100.0),
        ])
        # P0-05：value 按 有效权重归一；全 missing（effective==0）时为 None，
        # 域类型冻结 → 记 0 + confidence=0（reasons 已含全部 :missing）。
        value = 0.0 if value is None else value
        penalty = (
            self.penalty_engine.evaluate(stock)
            if self.penalty_engine is not None else PenaltyAssessment(total=0.0)
        )
        net = max(0.0, min(value + penalty.total, 100.0))
        return SoftOpportunityResult(
            value=value,
            net_value=round(net, 4),
            confidence=confidence,
            reasons=reasons,
            penalty=penalty,
            features_used=features_used,
        )

    @staticmethod
    def _nonchase(view) -> float | None:
        """§19.2 NonChase：奖励「刚启动、未脱离成本区」。

        v1 代理：20 日涨幅不追高 + 连涨天数克制 + 无爆量。
        各子代理可 missing，有效者取均值。
        """
        f = view.number
        parts: list[float] = []
        return_20d = f("return_20d")
        if is_finite(return_20d):
            parts.append(soft_low_better(return_20d, 0.08, 0.30) / 100.0)  # type: ignore[arg-type]
        up_days = f("consecutive_up_days")
        if is_finite(up_days):
            parts.append(soft_low_better(float(up_days), 3.0, 8.0) / 100.0)  # type: ignore[arg-type]
        spike = f("volume_spike")
        if is_finite(spike):
            parts.append(soft_low_better(spike, 1.5, 3.0) / 100.0)  # type: ignore[arg-type]
        return sum(parts) / len(parts) if parts else None
