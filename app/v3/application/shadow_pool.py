"""Step 18：Shadow Pool 分层抽样（设计 §31.2）。

每天从被淘汰池抽 200~300：
- near_miss 100：死在 MACHINE（rank 最接近 top_n）或 PARETO front≤3
- single_expert 50：单专家 rank≤3 命中但最终落选
- random 50：其余被淘汰随机对照
- other 50：风险样本（SAFETY 淘汰属资格类风险）

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

__all__ = ["ShadowPoolService"]


class ShadowPoolService:
    def __init__(
        self,
        sizes: dict[str, int] | None = None,
        seed: int = 42,
        near_miss_front_ceiling: int = 3,
        single_expert_rank_ceiling: int = 3,
    ) -> None:
        self.sizes = sizes or {
            "near_miss": 100, "single_expert": 50, "random": 50, "other": 50,
        }
        self._rng = random.Random(seed)
        self._near_miss_front = near_miss_front_ceiling
        self._single_rank = single_expert_rank_ceiling

    def sample(self, views: list[TraceView]) -> list[ShadowSampleEntry]:
        dead = [view for view in views if view.drop_stage is not None]
        pools = self._stratify(dead)
        chosen: list[ShadowSampleEntry] = []
        used: set = set()
        for group in SHADOW_GROUPS:
            size = self.sizes.get(group, 0)
            pool = [view for view in pools[group] if view.security_id not in used]
            self._rng.shuffle(pool)
            for view in pool[:size]:
                used.add(view.security_id)
                chosen.append(ShadowSampleEntry(
                    code=view.code,
                    security_id=view.security_id,
                    sample_group=group,
                    drop_stage=view.drop_stage,
                    drop_reason=view.drop_reason,
                ))
        return chosen

    def _stratify(self, dead: list[TraceView]) -> dict[str, list]:
        pools: dict[str, list] = {group: [] for group in SHADOW_GROUPS}
        single_expert_ids = self._single_expert_ids(dead)
        near_rank_ceiling = max(self.sizes.get("near_miss", 100), 100)
        for view in dead:
            # §31.2 near_miss：淘汰位置离入选最近
            if (
                view.drop_stage == "MACHINE"
                and (view.machine_rank or 10**9) <= near_rank_ceiling
            ):
                pools["near_miss"].append(view)
            elif (
                view.drop_stage == "PARETO"
                and (view.pareto_front or 10**9) <= self._near_miss_front
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
