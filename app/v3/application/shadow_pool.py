"""Step 18：Shadow Pool 分层抽样（设计 §31.2，第二轮 P0-10 修正）。

每天从被淘汰池抽 200~300：
- near_miss 100：死在 MACHINE（machine_top_n < rank <= machine_top_n+window，
  即 Top120 之外紧邻落选的 121~220）或死在 PARETO（front≤5 且未入选）
- single_expert 50：单专家 rank≤3 命中但最终落选
- random 50：其余被淘汰随机对照
- other 50：风险样本（SAFETY 淘汰属资格类风险）

P0-10 修正：
- 旧 near_rank_ceiling=max(size,100) 恒 <= machine_top_n，被 Machine
  淘汰者 rank>top_n 永不命中 → 改为显式 (top_n, top_n+window] 窗口；
- Pareto near miss 由 front≤3 收宽为 front≤5 且必须 selected=False；
- 组配额不足时 fallback reallocation：缺口转给 near_miss/random，
  sample_group 保持原语义，quota_reallocation_from_<group> 记入
  sample_reason（落库拼进 drop_reason，免 schema 变更）；
- 总量不足 SHADOW_TARGET_MIN 时置 shortfall_reason（不静默）。

seed 可复现（回放/审计要求）；20 日后与 Final 组比较
MFE/MAE/GOOD rate（比较由 metrics 查询端完成）。

输入统一为 TraceView（内存 pipeline 与落库快照两条路径共用）。
"""

from __future__ import annotations

import random

from app.v3.application.trace_view import TraceView
from app.v3.domain.candidate_engine import (
    SHADOW_GROUPS,
    ShadowSampleEntry,
)

__all__ = ["ShadowPoolService", "SHADOW_TARGET_MIN"]

SHADOW_TARGET_MIN = 200  # 目标 200~300，不足必须报 shortfall_reason


class ShadowPoolService:
    def __init__(
        self,
        sizes: dict[str, int] | None = None,
        seed: int = 42,
        near_miss_front_ceiling: int = 5,
        single_expert_rank_ceiling: int = 3,
        machine_top_n: int = 120,
        near_miss_window: int = 100,
    ) -> None:
        self.sizes = sizes or {
            "near_miss": 100, "single_expert": 50, "random": 50, "other": 50,
        }
        self._rng = random.Random(seed)
        self._near_miss_front = near_miss_front_ceiling
        self._single_rank = single_expert_rank_ceiling
        self._machine_top_n = machine_top_n
        self._near_miss_window = near_miss_window
        # sample() 后可读；None = 未不足（调用方写进 summary 上报）
        self.last_shortfall_reason: str | None = None

    def sample(self, views: list[TraceView]) -> list[ShadowSampleEntry]:
        dead = [view for view in views if view.drop_stage is not None]
        pools = self._stratify(dead)
        chosen: list[ShadowSampleEntry] = []
        used: set = set()
        shortfall: dict[str, int] = {}
        # 第一轮：按组配额抽取
        for group in SHADOW_GROUPS:
            size = self.sizes.get(group, 0)
            pool = self._unused(pools[group], used)
            self._rng.shuffle(pool)
            take = pool[:size]
            used.update(view.security_id for view in take)
            chosen.extend(self._entries(take, group))
            if len(take) < size:
                shortfall[group] = size - len(take)
        # 第二轮：缺口 fallback reallocation（转 near_miss → random），
        # 组语义不被篡改，来源记录在 sample_reason。
        for source_group, miss in shortfall.items():
            for target in ("near_miss", "random"):
                if miss <= 0:
                    break
                pool = self._unused(pools[target], used)
                self._rng.shuffle(pool)
                take = pool[:miss]
                used.update(view.security_id for view in take)
                chosen.extend(self._entries(
                    take, target,
                    reason=f"quota_reallocation_from_{source_group}",
                ))
                miss -= len(take)
        quota = sum(self.sizes.values())
        if len(chosen) < SHADOW_TARGET_MIN:
            group_stat = ",".join(
                f"{group}={sum(1 for e in chosen if e.sample_group == group)}"
                for group in SHADOW_GROUPS
            )
            self.last_shortfall_reason = (
                f"shadow_shortfall:{len(chosen)}/{quota}"
                f"(target_min={SHADOW_TARGET_MIN};{group_stat})"
            )
        else:
            self.last_shortfall_reason = None
        return chosen

    def _stratify(self, dead: list[TraceView]) -> dict[str, list]:
        pools: dict[str, list] = {group: [] for group in SHADOW_GROUPS}
        single_expert_ids = self._single_expert_ids(dead)
        near_floor = self._machine_top_n + 1
        near_ceiling = self._machine_top_n + self._near_miss_window
        for view in dead:
            # §31.2 near_miss：淘汰位置离入选最近——Machine Top120 之外
            # 紧邻窗口 (120, 220]（P0-10：旧逻辑 rank<=100 恒假）。
            if (
                view.drop_stage == "MACHINE"
                and view.machine_rank is not None
                and near_floor <= view.machine_rank <= near_ceiling
            ):
                pools["near_miss"].append(view)
            elif (
                view.drop_stage == "PARETO"
                and view.pareto_front is not None
                and view.pareto_front <= self._near_miss_front
                and not view.pareto_selected
            ):
                pools["near_miss"].append(view)
            elif view.security_id in single_expert_ids:
                pools["single_expert"].append(view)
            elif view.drop_stage == "SAFETY":
                pools["other"].append(view)  # 资格类淘汰属风险样本
            else:
                pools["random"].append(view)
        return pools

    def _single_expert_ids(self, dead: list[TraceView]) -> set:
        """单专家 Top3 命中但未进 Final 的股票（§18/§31.2）。"""
        ids: set = set()
        for view in dead:
            if view.final or not view.expert_ranks:
                continue
            if any(rank <= self._single_rank for rank in view.expert_ranks.values()):
                # 单专家：只有一个专家命中
                if len(view.expert_ranks) == 1:
                    ids.add(view.security_id)
        return ids

    @staticmethod
    def _unused(pool: list[TraceView], used: set) -> list[TraceView]:
        return [view for view in pool if view.security_id not in used]

    @staticmethod
    def _entries(
        pool: list[TraceView], group: str, *, reason: str | None = None,
    ) -> list[ShadowSampleEntry]:
        return [
            ShadowSampleEntry(
                code=view.code,
                security_id=view.security_id,
                sample_group=group,
                drop_stage=view.drop_stage,
                drop_reason=view.drop_reason,
                sample_reason=reason,
            )
            for view in pool
        ]
