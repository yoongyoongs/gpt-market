"""Step 17：Recall@K / Precision@K / NDCG@K（设计 §27-§29；任务书 §14）。

纯计算：输入 = 各阶段全量 ranking + Recall/Pareto 池 + Universe 标签。

P0-12 Pool 语义：
- MachineRanking = MACHINE 所有有 rank 的行（不限 alive），
  Top300 与 Top120 才真正可分；
- Precision@10/20/30 用 Final ranking，@60 用 Deep Top60；
  源不足 K → NOT_APPLICABLE（value=None），不除以 K 硬凑；
- NDCG 的 IDCG 来自 eligible 且已成熟的 ground truth 理想序，
  漏掉的 A/B 必须能压低 NDCG（旧版 IDCG 取 ranking 自身 gains）；
- 观察窗未成熟 → status=PENDING / value=None，不返回伪装成真实差的 0。
"""

from __future__ import annotations

import math
from uuid import UUID

from app.v3.domain.candidate_engine import (
    GOOD_LABELS,
    BacktestMetricsResult,
    MetricsEntry,
    OutcomeLabelResult,
)

__all__ = ["BacktestMetricsService"]

_PENDING = ("PENDING", "OUTCOME_WINDOW_NOT_MATURE")


def _gain(label: str | None) -> float:
    if label == "A":
        return 3.0
    if label == "B":
        return 2.0
    if label == "C":
        return 1.0
    return 0.0


def _sorted_by_rank(members: list[tuple[str, int]]) -> list[tuple[str, int]]:
    return sorted(members, key=lambda item: item[1])


class BacktestMetricsService:
    """rankings: {source: [(code, rank)]}（各层全量，1-based）。

    ground truth 域 = eligible_codes（默认全部已标股票）中 label 非 None 者。
    """

    RECALL_POOLS = ("recall_pool", "pareto_pool")
    PRECISION_SOURCES = ((10, "final"), (20, "final"), (30, "final"), (60, "deep"))
    NDCG_SOURCES = ("machine", "deep", "final")
    NDCG_KS = (10, 30, 100)

    def compute(
        self,
        labels: list[OutcomeLabelResult],
        pool_members: dict[str, list[tuple[str, int]]],
        *,
        rankings: dict[str, list[tuple[str, int]]] | None = None,
        scan_id: UUID | None = None,
        eligible_codes: set[str] | None = None,
    ) -> BacktestMetricsResult:
        rankings = rankings or {}
        labels_by_code = {entry.code: entry.label for entry in labels}
        if eligible_codes is None:
            eligible_codes = set(labels_by_code)

        # ground truth：域内已成熟标签的 gain 理想序（降序）
        gt_gains = sorted(
            (
                _gain(label)
                for code, label in labels_by_code.items()
                if code in eligible_codes and label is not None
            ),
            reverse=True,
        )
        matured = bool(gt_gains)
        good_count = sum(
            1 for entry in labels
            if entry.code in eligible_codes and entry.label in GOOD_LABELS
        )

        machine = _sorted_by_rank(rankings.get("machine") or [])
        deep = _sorted_by_rank(rankings.get("deep") or [])
        final = _sorted_by_rank(rankings.get("final") or [])
        derived_pools = {
            "top300_machine": machine[:300],
            "top120_machine": machine[:120],
            "top60_deep": deep[:60],
            "top30_final": final[:30],
        }

        entries: list[MetricsEntry] = []

        # ---- Recall：显式池 + 由全量 ranking 派生的 TopK 池 ----
        for pool_name in (*self.RECALL_POOLS, *derived_pools):
            members = (pool_members.get(pool_name) or derived_pools.get(pool_name) or [])
            numerator = sum(
                1 for code, _ in members
                if code in eligible_codes and labels_by_code.get(code) in GOOD_LABELS
            )
            if not matured:
                value, status, reason = None, _PENDING[0], _PENDING[1]
            elif good_count == 0:
                value, status, reason = None, "NOT_APPLICABLE", "NO_GOOD_IN_GROUND_TRUTH"
            else:
                value, status, reason = numerator / good_count, "OK", None
            entries.append(MetricsEntry(
                metric="recall", k=len(members),
                value=value, numerator=numerator, denominator=good_count,
                pool=pool_name, status=status, reason=reason,
            ))

        # ---- Precision：排名来源显式（@60 用 Deep，不拿 Final30 硬凑）----
        for k, source in self.PRECISION_SOURCES:
            ranking = {"final": final, "deep": deep}[source]
            numerator = 0
            if not matured:
                value, status, reason = None, _PENDING[0], _PENDING[1]
            elif len(ranking) < k:
                value, status, reason = None, "NOT_APPLICABLE", "POOL_SMALLER_THAN_K"
            else:
                head = ranking[:k]
                numerator = sum(
                    1 for code, _ in head
                    if code in eligible_codes and labels_by_code.get(code) in GOOD_LABELS
                )
                value, status, reason = numerator / k, "OK", None
            entries.append(MetricsEntry(
                metric="precision", k=k,
                value=value, numerator=numerator, denominator=k,
                ranking_source=source, status=status, reason=reason,
            ))

        # ---- NDCG：IDCG 来自 ground truth 理想序（漏选必受罚）----
        for source in self.NDCG_SOURCES:
            ranking = {"machine": machine, "deep": deep, "final": final}[source]
            gains = [_gain(labels_by_code.get(code)) for code, _ in ranking]
            for k in self.NDCG_KS:
                if not matured:
                    value, status, reason = None, _PENDING[0], _PENDING[1]
                else:
                    idcg = sum(
                        (2.0 ** gain - 1.0) / math.log2(index + 2)
                        for index, gain in enumerate(gt_gains[:k])
                    )
                    if idcg <= 0:
                        value, status, reason = None, "NOT_APPLICABLE", "NO_GOOD_IN_GROUND_TRUTH"
                    else:
                        dcg = sum(
                            (2.0 ** gain - 1.0) / math.log2(index + 2)
                            for index, gain in enumerate(gains[:k])
                        )
                        value, status, reason = dcg / idcg, "OK", None
                entries.append(MetricsEntry(
                    metric="ndcg", k=k,
                    value=value, numerator=0, denominator=0,
                    ranking_source=source, status=status, reason=reason,
                ))

        return BacktestMetricsResult(
            scan_id=scan_id,
            good_count=good_count,
            labeled_count=sum(1 for entry in labels if entry.label is not None),
            entries=tuple(entries),
        )


def is_good_label(label: str | None) -> bool:
    return label in GOOD_LABELS
