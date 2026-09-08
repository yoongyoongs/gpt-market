"""Step 17：False Negative / 漏选审计（设计 §30）。

任何未来成为 A/B 的股票若未进 Final，自动生成 miss audit：
last_alive_stage / drop_stage / drop_reason 来自全链 Trace，
audit dict 记录 expert 命中名次、Pareto front、Machine rank 等
「为什么当时没进」的可解释证据。

输入统一为 TraceView（内存 pipeline 与落库快照两条路径共用）。
"""

from __future__ import annotations

from app.v3.application.trace_view import TraceView
from app.v3.domain.candidate_engine import (
    GOOD_LABELS,
    MissAuditEntry,
    OutcomeLabelResult,
)

__all__ = ["MissAuditService"]


class MissAuditService:
    def collect(
        self,
        views: list[TraceView],
        labels: list[OutcomeLabelResult],
    ) -> list[MissAuditEntry]:
        final_codes = {view.code for view in views if view.final}
        good_not_final = [
            entry for entry in labels
            if entry.label in GOOD_LABELS and entry.code not in final_codes
        ]
        by_code = {view.code: view for view in views}
        entries: list[MissAuditEntry] = []
        for label in good_not_final:
            view = by_code.get(label.code)
            if view is None:
                continue
            entries.append(MissAuditEntry(
                code=label.code,
                security_id=view.security_id,
                future_label=label.label or "GOOD",
                last_alive_stage=view.last_alive_stage,
                drop_stage=view.drop_stage,
                drop_reason=view.drop_reason,
                audit=self._evidence(view, label),
            ))
        entries.sort(key=lambda entry: entry.code)
        return entries

    @staticmethod
    def _evidence(view: TraceView, label: OutcomeLabelResult) -> dict:
        """§30 示例字段：LP #31 / Pareto Front2 / Machine #167 / 主因。"""
        evidence: dict = {
            "mfe_20": label.mfe_20,
            "mae_20": label.mae_20,
            "time_to_10": label.time_to_10,
        }
        if view.expert_ranks:
            evidence["experts"] = sorted(view.expert_ranks)
            best = min(view.expert_ranks.values())
            evidence["union_rank"] = best
        if view.pareto_front is not None:
            evidence["pareto_front"] = view.pareto_front
            evidence["protected"] = view.pareto_protected
        if view.machine_rank is not None:
            evidence["machine_rank"] = view.machine_rank
        return evidence
