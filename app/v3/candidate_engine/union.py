"""L2 Recall Union + RRF 融合（设计 §13-§14）。

Union：八路专家结果并集，不设「命中最少专家数」（§13.2）——
单路命中且排名足够高同样保留。

RRF：rrf = Σ W_i / (K + rank_i)，第一版 K=60、W_i=1.0；
归一化 100*(rrf-min)/(max-min)，max==min 退化时全取 100。
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from app.v3.domain.candidate_engine import ExpertHit, RecallUnionResult, UnionEntry

__all__ = ["RecallUnionService", "DEFAULT_RRF_K"]

DEFAULT_RRF_K = 60


class RecallUnionService:
    """RRF 融合器（通道集合无关：喂多少路专家都成立）。"""

    def __init__(self, k: int = DEFAULT_RRF_K, weights: dict[str, float] | None = None):
        if k <= 0:
            raise ValueError("RRF K must be positive")
        self.k = k
        self.weights = weights or {}

    def execute(
        self,
        expert_results: dict[str, tuple[ExpertHit, ...]],
        *,
        evaluated_count: int | None = None,
    ) -> RecallUnionResult:
        """合并各专家 TopN 命中并计算 RRF。

        expert_results: {专家名: 该专家命中列表（rank 1-based）}。
        RRF 依据 ExpertHit.rank 计算，与专家打分绝对值无关。
        """
        rrf_by_id: dict[UUID, float] = defaultdict(float)
        hits_by_id: dict[UUID, list[ExpertHit]] = defaultdict(list)
        code_by_id: dict[UUID, str] = {}
        for expert_name, hits in expert_results.items():
            weight = self.weights.get(expert_name, 1.0)
            for hit in hits:
                rrf_by_id[hit.security_id] += weight / (self.k + hit.rank)
                hits_by_id[hit.security_id].append(hit)
                code_by_id.setdefault(hit.security_id, hit.code)

        if not rrf_by_id:
            return RecallUnionResult(
                evaluated_count=evaluated_count or 0, union_count=0, entries=()
            )

        rrf_values = list(rrf_by_id.values())
        max_rrf, min_rrf = max(rrf_values), min(rrf_values)
        span = max_rrf - min_rrf

        entries: list[UnionEntry] = []
        for security_id, rrf in rrf_by_id.items():
            hits = tuple(sorted(hits_by_id[security_id], key=lambda h: h.rank))
            norm = 100.0 if span == 0 else 100.0 * (rrf - min_rrf) / span
            entries.append(
                UnionEntry(
                    security_id=security_id,
                    code=code_by_id[security_id],
                    hits=hits,
                    expert_names=tuple(hit.expert for hit in hits),
                    best_score=max(hit.score for hit in hits),
                    rrf_raw=rrf,
                    rrf_norm=round(norm, 6),
                    union_rank=1,  # 排序后重写
                )
            )
        entries.sort(key=lambda e: (-e.rrf_raw, -e.best_score, e.code))
        ordered = tuple(
            entry.model_copy(update={"union_rank": rank})
            for rank, entry in enumerate(entries, start=1)
        )
        return RecallUnionResult(
            evaluated_count=evaluated_count or len({h.security_id for hits in expert_results.values() for h in hits}),
            union_count=len(ordered),
            entries=ordered,
        )
