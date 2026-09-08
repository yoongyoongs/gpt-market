"""Step 17：Recall@K / Precision@K / NDCG@K（设计 §27-§29）。

纯计算：输入 = 各阶段池成员排名 + Universe 全量标签。
Recall 分母 = Universe 全部 GOOD_OPPORTUNITY（A/B）；
Precision@K = Final 排名前 K 中 GOOD 数 / K；
NDCG@K：gain A=3/B=2/C=1，DCG = Σ(2^rel-1)/log2(i+1)。
"""

from __future__ import annotations

import math
from uuid import UUID

from app.v3.domain.candidate_engine import (
    BacktestMetricsResult,
    GOOD_LABELS,
    MetricsEntry,
    OutcomeLabelResult,
)

__all__ = ["BacktestMetricsService"]


def _gain(label: str | None) -> float:
    if label == "A":
        return 3.0
    if label == "B":
        return 2.0
    if label == "C":
        return 1.0
    return 0.0


class BacktestMetricsService:
    """pool_members: {pool_name: [(code, rank)]}；rank 1-based。"""

    RECALL_POOLS = ("recall_pool", "pareto_pool", "top300", "top120", "top60", "top30")
    PRECISION_KS = (10, 20, 30, 60)
    NDCG_KS = (10, 30, 100)

    def compute(
        self,
        labels: list[OutcomeLabelResult],
        pool_members: dict[str, list[tuple[str, int]]],
        *,
        scan_id: UUID | None = None,
    ) -> BacktestMetricsResult:
        good_codes = {entry.code for entry in labels if entry.is_good}
        entries: list[MetricsEntry] = []

        for pool_name in self.RECALL_POOLS:
            members = pool_members.get(pool_name) or []
            numerator = sum(1 for code, _ in members if code in good_codes)
            denominator = len(good_codes)
            entries.append(MetricsEntry(
                metric="recall", k=len(members),
                value=numerator / denominator if denominator else 0.0,
                numerator=numerator, denominator=denominator,
                pool=pool_name,
            ))

        ranking = sorted(pool_members.get("final") or [], key=lambda item: item[1])
        for k in self.PRECISION_KS:
            head = ranking[:k]
            numerator = sum(1 for code, _ in head if code in good_codes)
            entries.append(MetricsEntry(
                metric="precision", k=k,
                value=numerator / k if k else 0.0,
                numerator=numerator, denominator=k,
            ))

        gains = [_gain(self._label_of(code, labels)) for code, _ in ranking]
        for k in self.NDCG_KS:
            dcg = sum(
                (2.0 ** gain - 1.0) / math.log2(index + 2)
                for index, gain in enumerate(gains[:k])
            )
            ideal = sorted(gains, reverse=True)[:k]
            idcg = sum(
                (2.0 ** gain - 1.0) / math.log2(index + 2)
                for index, gain in enumerate(ideal)
            )
            entries.append(MetricsEntry(
                metric="ndcg", k=k,
                value=dcg / idcg if idcg > 0 else 0.0,
                numerator=0, denominator=0,
            ))

        return BacktestMetricsResult(
            scan_id=scan_id,
            good_count=len(good_codes),
            labeled_count=sum(1 for entry in labels if entry.label is not None),
            entries=tuple(entries),
        )

    @staticmethod
    def _label_of(code: str, labels: list[OutcomeLabelResult]) -> str | None:
        for entry in labels:
            if entry.code == code:
                return entry.label
        return None


def is_good_label(label: str | None) -> bool:
    return label in GOOD_LABELS
