"""L4 Pareto 多目标保护 + 单专家极强保护（设计 §16/§18）。

5 个战略维度（P1 Position / P2 Transition / P3 Accumulation /
P4 QualityCatalyst / P5 RiskReward），0~100 越高越好。

维度缺失语义（设计 §16.2 P4「不能简单置零」的推广）：
缺失维以中性分 50 参与支配比较并记 missing_dimensions——
不奖励、不惩罚；某维全体缺失（如 v1 的 P5）时该维无区分度，
Pareto 自然退化为有效维比较。

目标数量 250~400（§16.4）：F1~F3 超 400 用 crowding+RRF 截断，
不足 250 依次追加 F4/F5。单专家保护 reserve_ratio=0.15，
保护来源 = 各专家 Top10 未入选命中，去重后并入（§18）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.v3.domain.candidate_engine import (
    CROWDING_BOUNDARY,
    NEUTRAL_DIMENSION_SCORE,
    PARETO_DIMENSIONS,
    ExpertHit,
    ParetoCandidateInput,
    ParetoEntry,
    ParetoResult,
)

__all__ = ["ParetoConfig", "ParetoService", "SingleExpertProtection"]


@dataclass(frozen=True)
class ParetoConfig:
    target_min: int = 250
    target_max: int = 400
    reserve_ratio: float = 0.15
    neutral_score: float = NEUTRAL_DIMENSION_SCORE


@dataclass
class _Ranked:
    """内部排序辅助（可变）；ParetoEntry 冻结，出口一次性构建。"""

    security_id: UUID
    code: str
    rrf_norm: float
    scores_input: dict[str, float | None]
    scores: dict[str, float]
    front: int = 0
    crowding: float = 0.0
    selected: bool = False
    dominated_by: set[int] = field(default_factory=set)

    def to_entry(self, protected: ParetoEntry | None = None) -> ParetoEntry:
        if protected is not None:
            return protected
        return ParetoEntry(
            security_id=self.security_id,
            code=self.code,
            rrf_norm=self.rrf_norm,
            scores=dict(self.scores_input),
            front=self.front,
            crowding=self.crowding,
            selected=self.selected,
        )


class ParetoService:
    """Non-dominated sorting + crowding distance + 目标数量控制。"""

    def __init__(self, config: ParetoConfig | None = None):
        self.config = config or ParetoConfig()

    def execute(
        self,
        candidates: list[ParetoCandidateInput],
        expert_results: dict[str, tuple[ExpertHit, ...]] | None = None,
    ) -> ParetoResult:
        neutral = self.config.neutral_score
        items = [
            _Ranked(
                security_id=candidate.security_id,
                code=candidate.code,
                rrf_norm=candidate.rrf_norm,
                scores_input=dict(candidate.scores),
                scores={
                    dim: (
                        neutral if candidate.dimension(dim) is None
                        else float(candidate.dimension(dim))  # type: ignore[arg-type]
                    )
                    for dim in PARETO_DIMENSIONS
                },
            )
            for candidate in candidates
        ]

        self._assign_fronts(items)
        front_sizes = self._front_sizes(items)
        selected = self._select(items, front_sizes)
        self._assign_crowding(selected)
        for item in selected:
            item.selected = True

        protection = SingleExpertProtection(self.config)
        protected_entries = protection.collect(
            expert_results or {}, {item.security_id for item in selected}
        )
        protected_by_id = {entry.security_id: entry for entry in protected_entries}

        entries = [item.to_entry(protected_by_id.get(item.security_id)) for item in sorted(
            items, key=lambda it: (it.front, -it.rrf_norm, it.code)
        )]
        entries.extend(
            entry for entry in protected_entries
            if entry.security_id not in {it.security_id for it in items}
        )
        return ParetoResult(
            evaluated_count=len(candidates),
            selected_count=len(selected),
            protected_count=len(protected_entries),
            front_sizes=front_sizes,
            entries=tuple(entries),
        )

    # ------------------------------------------------------------------
    def _assign_fronts(self, items: list[_Ranked]) -> None:
        """O(n²·d) 支配计数分层（量级 1e3 可接受，先正确后优化）。"""
        n = len(items)
        for i in range(n):
            for j in range(i + 1, n):
                relation = _dominates(items[i].scores, items[j].scores)
                if relation > 0:
                    items[j].dominated_by.add(i)
                elif relation < 0:
                    items[i].dominated_by.add(j)
        remaining = set(range(n))
        front = 0
        while remaining:
            front += 1
            current = {
                i for i in remaining
                if not (items[i].dominated_by & remaining)
            }
            if not current:  # 理论不可达（支配无环），防御
                current = set(remaining)
            for i in current:
                items[i].front = front
            remaining -= current

    def _front_sizes(self, items: list[_Ranked]) -> tuple[int, ...]:
        counts: dict[int, int] = {}
        for item in items:
            counts[item.front] = counts.get(item.front, 0) + 1
        return tuple(counts[front] for front in sorted(counts))

    def _select(self, items: list[_Ranked], front_sizes: tuple[int, ...]) -> list[_Ranked]:
        """F1..F3 达标；< target_min 追加 F4/F5；> target_max 截断
        （F1~F3 阶段 crowding 未算，先按 RRF；截断后统一算 crowding）。"""
        target_min, target_max = self.config.target_min, self.config.target_max
        by_front: dict[int, list[_Ranked]] = {}
        for item in items:
            by_front.setdefault(item.front, []).append(item)
        selected: list[_Ranked] = []
        front = 0
        while front < len(front_sizes):
            front += 1
            members = sorted(
                by_front.get(front, []), key=lambda it: (-it.rrf_norm, it.code)
            )
            if front <= 3 and len(selected) + len(members) > target_max:
                members = members[: max(0, target_max - len(selected))]
            selected.extend(members)
            if len(selected) >= target_min:
                break
        return selected[:target_max]

    @staticmethod
    def _assign_crowding(selected: list[_Ranked]) -> None:
        """NSGA-II crowding distance（按 front 分组计算）。"""
        by_front: dict[int, list[_Ranked]] = {}
        for item in selected:
            by_front.setdefault(item.front, []).append(item)
        for members in by_front.values():
            if len(members) <= 2:
                for member in members:
                    member.crowding = CROWDING_BOUNDARY
                continue
            for dim in PARETO_DIMENSIONS:
                members.sort(key=lambda it: it.scores[dim])
                low, high = members[0].scores[dim], members[-1].scores[dim]
                span = high - low
                members[0].crowding = CROWDING_BOUNDARY
                members[-1].crowding = CROWDING_BOUNDARY
                if span <= 0:
                    continue
                for index in range(1, len(members) - 1):
                    if members[index].crowding >= CROWDING_BOUNDARY:
                        continue
                    members[index].crowding += (
                        members[index + 1].scores[dim] - members[index - 1].scores[dim]
                    ) / span


def _dominates(a: dict[str, float], b: dict[str, float]) -> int:
    """a 支配 b 返回 1，b 支配 a 返回 -1，互不支配返回 0。"""
    if all(a[d] >= b[d] for d in PARETO_DIMENSIONS) and any(
        a[d] > b[d] for d in PARETO_DIMENSIONS
    ):
        return 1
    if all(a[d] <= b[d] for d in PARETO_DIMENSIONS) and any(
        a[d] < b[d] for d in PARETO_DIMENSIONS
    ):
        return -1
    return 0


class SingleExpertProtection:
    """单专家极强保护（设计 §18）：各专家 Top10 未入选命中
    依专家序填入 reserve 名额，去重。"""

    def __init__(self, config: ParetoConfig):
        self.config = config

    def collect(
        self,
        expert_results: dict[str, tuple[ExpertHit, ...]],
        already_selected: set[UUID],
    ) -> list[ParetoEntry]:
        quota = max(0, int(round(self.config.reserve_ratio * self.config.target_max)))
        entries: list[ParetoEntry] = []
        seen = set(already_selected)
        for expert_name, hits in expert_results.items():
            for hit in hits:
                if len(entries) >= quota:
                    return entries
                if hit.rank > 10:
                    break
                if hit.security_id in seen:
                    continue
                seen.add(hit.security_id)
                entries.append(ParetoEntry(
                    security_id=hit.security_id,
                    code=hit.code,
                    rrf_norm=0.0,
                    scores={},
                    protected=True,
                    protected_reason=f"single_expert:{expert_name}#rank{hit.rank}",
                ))
        return entries
