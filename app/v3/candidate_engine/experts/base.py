"""召回专家基座（设计 §5.1 注入式专家 + 任务书 §6 统一输出）。"""

from __future__ import annotations

from uuid import UUID

from app.v3.candidate_engine.soft import weighted_combine
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
        """按设计权重合成分数（P0-05：missing 归一不双罚，§11.3）。

        全 missing（effective==0）时 weighted_combine 返回 value=None，
        ExpertScore.value 域类型冻结非 None → 记 0 分 + confidence=0，
        下游按 confidence==0 判 missing（任务书 §7.5 允许路径）。"""
        value, confidence, reasons, features_used = weighted_combine(
            parts, extra_confidence=extra_confidence
        )
        return ExpertScore(
            value=0.0 if value is None else value, confidence=confidence,
            reasons=reasons, features_used=features_used,
        )
