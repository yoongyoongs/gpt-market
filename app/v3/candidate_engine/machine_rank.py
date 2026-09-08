"""L5 Machine Rank（设计 §21）。

MachineRankScore = 0.35*RecallFusion(rrf_norm)
                 + 0.50*SoftOpportunity(net，扣 Penalty)
                 + 0.15*ParetoQuality(front 分档 + crowding 微调)。
输出 Top120（配置 100~150）。
"""

from __future__ import annotations

from uuid import UUID

from app.v3.domain.candidate_engine import CROWDING_BOUNDARY, MachineRankEntry, MachineRankResult

__all__ = ["MachineRankService", "MachineRankConfig", "pareto_quality"]

FRONT_QUALITY = {1: 100.0, 2: 85.0, 3: 70.0, 4: 55.0, 5: 40.0}
CROWDING_BONUS = 5.0  # 边界解（多样性保留者）轻微加分


def pareto_quality(front: int, crowding: float) -> float:
    base = FRONT_QUALITY.get(front)
    if base is None:
        base = max(0.0, 40.0 - 10.0 * (front - 5))
    if crowding >= CROWDING_BOUNDARY:
        base += CROWDING_BONUS
    return min(base, 100.0)


class MachineRankConfig:
    def __init__(self, top_n: int = 120, w_rrf: float = 0.35, w_soft: float = 0.50,
                 w_pareto: float = 0.15):
        if not 100 <= top_n <= 150:
            raise ValueError("Machine Rank top_n must be within 100~150 (design §21.3)")
        self.top_n = top_n
        self.w_rrf = w_rrf
        self.w_soft = w_soft
        self.w_pareto = w_pareto


class MachineRankService:
    """输入 = Union 命中 + Pareto 结果 + SoftOpportunity 净分。"""

    def __init__(self, config: MachineRankConfig | None = None):
        self.config = config or MachineRankConfig()

    def execute(
        self,
        pool: list[dict],
    ) -> MachineRankResult:
        """pool: [{security_id, code, rrf_norm, soft_net, front, crowding}]。"""
        scored: list[tuple[float, str, UUID, dict[str, float]]] = []
        for item in pool:
            quality = pareto_quality(item["front"], item.get("crowding", 0.0))
            score = (
                self.config.w_rrf * item["rrf_norm"]
                + self.config.w_soft * item["soft_net"]
                + self.config.w_pareto * quality
            )
            components = {
                "rrf": item["rrf_norm"],
                "soft_net": item["soft_net"],
                "pareto_quality": quality,
            }
            scored.append((score, item["code"], item["security_id"], components))
        scored.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
        entries: list[MachineRankEntry] = []
        for rank, (score, code, security_id, components) in enumerate(scored, start=1):
            entries.append(MachineRankEntry(
                security_id=security_id,
                code=code,
                machine_score=round(min(max(score, 0.0), 100.0), 4),
                components={key: round(value, 4) for key, value in components.items()},
                rank=rank,
                selected=rank <= self.config.top_n,
            ))
        return MachineRankResult(
            evaluated_count=len(pool),
            top_n=min(self.config.top_n, len(entries)),
            entries=tuple(entries),
        )
