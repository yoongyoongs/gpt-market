"""Step 17-18：轻量轨迹视图（内存 pipeline 与落库快照共用）。

Miss Audit / Shadow Pool 只需要每只股票的最终轨迹摘要，
不依赖完整 CandidatePipelineResult，便于落库路径（snapshots 表）
重建同一结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.v3.domain.candidate_engine import CandidatePipelineResult

__all__ = ["TraceView", "to_trace_views"]


@dataclass(frozen=True)
class TraceView:
    code: str
    security_id: UUID
    last_alive_stage: str | None
    drop_stage: str | None
    drop_reason: str | None
    machine_rank: int | None = None
    machine_selected: bool = False
    pareto_front: int | None = None
    pareto_selected: bool = False
    pareto_protected: bool = False
    expert_ranks: dict[str, int] = field(default_factory=dict)
    final: bool = False


def to_trace_views(result: CandidatePipelineResult) -> list[TraceView]:
    """内存路径：从 pipeline result 装配（扫描后立即生成审计/影子池）。"""
    pareto_by_id = {entry.security_id: entry for entry in result.pareto.entries}
    machine_by_id = {entry.security_id: entry for entry in result.machine.entries}
    final_ids = {entry.security_id for entry in result.final_entries}
    expert_ranks: dict[UUID, dict[str, int]] = {}
    for entry in result.union.entries:
        ranks = {}
        for hit in entry.hits:
            ranks[hit.expert] = hit.rank
        expert_ranks[entry.security_id] = ranks

    views: list[TraceView] = []
    for trace in result.trace.traces:
        pareto = pareto_by_id.get(trace.security_id)
        machine = machine_by_id.get(trace.security_id)
        views.append(TraceView(
            code=trace.code,
            security_id=trace.security_id,
            last_alive_stage=trace.last_alive_stage,
            drop_stage=trace.drop_stage,
            drop_reason=trace.drop_reason,
            machine_rank=machine.rank if machine else None,
            machine_selected=bool(machine and machine.selected),
            pareto_front=pareto.front if pareto else None,
            pareto_selected=bool(pareto and pareto.selected),
            pareto_protected=bool(pareto and pareto.protected),
            expert_ranks=expert_ranks.get(trace.security_id, {}),
            final=trace.security_id in final_ids,
        ))
    return views
