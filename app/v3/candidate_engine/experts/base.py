"""召回专家基座（设计 §5.1 注入式专家 + 任务书 §6 统一输出）。"""

from __future__ import annotations

from uuid import UUID

from app.v3.domain.candidate_engine import (
    ExpertFeatureView,
    ExpertHit,
    ExpertInput,
    ExpertScore,
    RecallExpert,
)

__all__ = [
    "BaseExpert",
    "ExpertFeatureView",
    "ExpertHit",
    "ExpertInput",
    "ExpertScore",
    "RecallExpert",
]


class BaseExpert:
    """专家公共执行骨架：评分 → 排序 → 截断 TopN → 统一命中结构。

    子类只实现 name/top_n/required_features/evaluate；打分为 None
    （核心特征缺失）的股票不进本专家召回，不算淘汰（设计 §13.2）。
    """

    def run(self, stocks: list[ExpertInput]) -> tuple[ExpertHit, ...]:
        hits: list[ExpertHit] = []
        scored: list[tuple[float, str, UUID, ExpertScore]] = []
        for stock in stocks:
            score = self.evaluate(stock)
            if score is None:
                continue
            scored.append((score.value, stock.feature.code, stock.feature.security_id, score))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        for rank, (value, code, security_id, score) in enumerate(scored[: self.top_n()], start=1):
            hits.append(
                ExpertHit(
                    expert=self.name(),
                    security_id=security_id,
                    code=code,
                    score=value,
                    rank=rank,
                    confidence=score.confidence,
                    reasons=score.reasons,
                    features=score.features_used,
                )
            )
        return tuple(hits)

    @staticmethod
    def cannot_evaluate(view: ExpertFeatureView, required: tuple[str, ...]) -> bool:
        """核心特征全部缺失 → 本专家不可评（返回 None，非 0 分）。

        required 为空（FQ/CAT 由证据驱动）时恒可评。
        """
        if not required:
            return False
        return all(view.number(key) is None and view.flag(key) is None for key in required)

    @staticmethod
    def combine(
        parts: list[tuple[str, float, float | None]],
        *,
        extra_confidence: float = 1.0,
    ) -> ExpertScore:
        """按设计权重合成分数。

        parts = [(维度名, 权重, 0~1 子分或 None)]；None 为 missing：
        计 0 分、权重不计入生效权重并按比例降低 confidence
        （设计 §11.3：missing => confidence 降低，不得 score=0 伪装）。
        """
        value = 0.0
        effective = 0.0
        reasons: list[str] = []
        features_used: dict[str, float | None] = {}
        for label, weight, subscore in parts:
            if subscore is None:
                features_used[label] = None
                reasons.append(f"{label}:missing")
                continue
            value += weight * max(0.0, min(1.0, subscore))
            effective += weight
            features_used[label] = round(subscore, 6)
            reasons.append(f"{label}:{subscore:.3f}*{weight:g}")
        total = sum(weight for _, weight, _ in parts)
        confidence = (effective / total if total > 0 else 0.0) * extra_confidence
        return ExpertScore(
            value=round(min(value, 100.0), 4),
            confidence=round(max(0.0, min(confidence, 1.0)), 4),
            reasons=tuple(reasons),
            features_used=features_used,
        )
