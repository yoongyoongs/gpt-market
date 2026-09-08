"""L4 Pareto 多目标保护 + 单专家极强保护（设计 §16/§18，第二轮 P0-01/P0-02）。

5 个战略维度（P1 Position / P2 Transition / P3 Accumulation /
P4 QualityCatalyst / P5 RiskReward），0~100 越高越好。

维度缺失语义（设计 §16.2 P4「不能简单置零」的推广）：
缺失维以中性分 50 参与支配比较并记 missing_dimensions——
不奖励、不惩罚；某维全体缺失（如 v1 的 P5）时该维无区分度，
Pareto 自然退化为有效维比较。

目标数量 250~400（§16.4）：
1. 先对全量候选做支配分层 + crowding distance（crowding 先于选择计算）；
2. F1~F(preserve_fronts) 作为基础保护区整体保留；
3. 保护区超 target_max 时按 crowding → RRF → code → security_id 截断；
4. 保护区不足 target_min 时依次追加 F4/F5/...，同样截断规则；
5. 单专家极强候选（hit_count==1 且 rank<=ceiling）经 swap-in 注入，
   总池仍受 target_max 约束，protected 保留真实 front/scores/rrf。
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
    preserve_fronts: int = 3
    reserve_ratio: float = 0.15
    single_expert_rank_ceiling: int = 10
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
    protected_reason: str | None = None
    dominated_by: set[int] = field(default_factory=set)

    def to_entry(self) -> ParetoEntry:
        return ParetoEntry(
            security_id=self.security_id,
            code=self.code,
            rrf_norm=self.rrf_norm,
            scores=dict(self.scores_input),
            front=self.front,
            crowding=self.crowding,
            selected=self.selected,
            protected=self.protected_reason is not None,
            protected_reason=self.protected_reason,
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
        # P0-01：crowding 先于选择、对全量候选按 front 分组计算，
        # 边界 Front 截断时 crowding 才能真正参与 survivor 选择。
        self._assign_crowding(items)
        base_selected = self._select(items)
        by_id = {item.security_id: item for item in items}

        protection = SingleExpertProtection(self.config)
        base_ids = {item.security_id for item in base_selected}
        protected = protection.collect(expert_results or {}, base_ids)

        # P0-02：swap-in 语义——protected 注入不扩池，从 base 移出
        # front 更深 / crowding 更低 / RRF 更低者补位。
        final = self._swap_in(base_selected, protected, by_id, target_max=self.config.target_max)
        for item in final:
            item.selected = True
        selected_ids = {item.security_id for item in final}

        # 不在候选集内的保护命中（理论上 Recall Union 已保证参与计算，
        # 兜底保留：front=0 语义留给未来真正外部注入的特殊情形）。
        orphan_entries: list[ParetoEntry] = []
        for security_id, reason in protected:
            if security_id in by_id or security_id in selected_ids:
                continue
            orphan_entries.append(ParetoEntry(
                security_id=security_id,
                code="",  # 调用方无此候选信息；由 trace/audit 层补充
                rrf_norm=0.0,
                scores={},
                front=0,
                crowding=0.0,
                selected=True,
                protected=True,
                protected_reason=reason,
            ))

        protected_count = sum(
            1 for security_id, _ in protected if security_id not in base_ids
        ) + len(orphan_entries)
        entries = [item.to_entry() for item in sorted(
            items, key=lambda it: (it.front, -it.rrf_norm, it.code)
        )]
        entries.extend(orphan_entries)
        return ParetoResult(
            evaluated_count=len(candidates),
            selected_count=len(selected_ids) + len(orphan_entries),
            protected_count=protected_count,
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

    @staticmethod
    def _member_key(item: _Ranked) -> tuple:
        """边界 Front 截断序（任务书 P0-01 §3.5）：
        crowding desc → rrf desc → code asc → security_id asc。"""
        return (-item.crowding, -item.rrf_norm, item.code, str(item.security_id))

    def _select(self, items: list[_Ranked]) -> list[_Ranked]:
        """F1~F(preserve_fronts) 全保护；超 target_max 按 crowding 截断；
        不足 target_min 依次追加更深 Front，同样截断规则。"""
        target_min, target_max = self.config.target_min, self.config.target_max
        by_front: dict[int, list[_Ranked]] = {}
        for item in items:
            by_front.setdefault(item.front, []).append(item)
        for members in by_front.values():
            members.sort(key=self._member_key)

        selected: list[_Ranked] = []
        # Phase B：基础保护区（F1~F3）。
        for front_no in range(1, self.config.preserve_fronts + 1):
            members = by_front.get(front_no)
            if not members:
                continue
            if len(selected) + len(members) <= target_max:
                selected.extend(members)
                continue
            remaining = target_max - len(selected)
            selected.extend(members[:remaining])
            return selected
        # Phase C：保护区不足 target_min，追加 F4/F5/...。
        max_front = max(by_front, default=0)
        front_no = self.config.preserve_fronts
        while len(selected) < target_min and front_no < max_front:
            front_no += 1
            members = by_front.get(front_no)
            if not members:
                continue
            if len(selected) + len(members) <= target_max:
                selected.extend(members)
            else:
                remaining = target_max - len(selected)
                selected.extend(members[:remaining])
                break
        return selected

    @staticmethod
    def _assign_crowding(items: list[_Ranked]) -> None:
        """NSGA-II crowding distance（按 front 分组、对全量候选计算）。"""
        by_front: dict[int, list[_Ranked]] = {}
        for item in items:
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

    def _swap_in(
        self,
        base: list[_Ranked],
        protected: list[tuple[UUID, str]],
        by_id: dict[UUID, _Ranked],
        *,
        target_max: int,
    ) -> list[_Ranked]:
        """把不在 base 的 protected 注入池内；超 target_max 时按
        front 深 → crowding 低 → RRF 低 的优先级从 base 移出补位。"""
        incoming = [(by_id[sid], reason) for sid, reason in protected if sid in by_id]
        incoming = [(item, reason) for item, reason in incoming if not item.selected]
        if not incoming:
            return list(base)
        overflow = len(base) + len(incoming) - target_max
        final = list(base)
        if overflow > 0:
            evict_order = sorted(
                base,
                key=lambda it: (-it.front, it.crowding, it.rrf_norm, it.code),
            )
            evicted = {it.security_id for it in evict_order[:overflow]}
            final = [it for it in final if it.security_id not in evicted]
        for item, reason in incoming:
            item.protected_reason = reason
            final.append(item)
        return final


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
    """单专家极强保护（设计 §18 / 第二轮任务书 P0-02 §4.4）。

    定义收严：仅 `hit_count == 1` 且 `expert_rank <= rank_ceiling`
    的未入选命中进入保护；多专家共同命中者由 Pareto 正常竞争。
    返回 (security_id, reason) 对，由 ParetoService 经 swap-in 注入
    并保留真实 front/scores/rrf（不再重建 front=0 的空壳 entry）。
    """

    def __init__(self, config: ParetoConfig):
        self.config = config

    def collect(
        self,
        expert_results: dict[str, tuple[ExpertHit, ...]],
        already_selected: set[UUID],
    ) -> list[tuple[UUID, str]]:
        hit_counts: dict[UUID, int] = {}
        for hits in expert_results.values():
            for hit in hits:
                hit_counts[hit.security_id] = hit_counts.get(hit.security_id, 0) + 1
        ceiling = self.config.single_expert_rank_ceiling
        quota = max(0, int(round(self.config.reserve_ratio * self.config.target_max)))
        found: list[tuple[UUID, str]] = []
        seen = set(already_selected)
        for expert_name in sorted(expert_results):
            for hit in expert_results[expert_name]:
                if hit.rank > ceiling:
                    break
                if hit_counts[hit.security_id] != 1:
                    continue
                if hit.security_id in seen:
                    continue
                seen.add(hit.security_id)
                found.append(
                    (hit.security_id, f"single_expert:{expert_name}#rank{hit.rank}")
                )
                if len(found) >= quota:
                    return found
        return found
