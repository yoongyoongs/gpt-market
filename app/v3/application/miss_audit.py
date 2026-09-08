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
        """任务书 §13 证据结构：LP #31 / Recall #44 / Pareto Front2 /
        Machine #167 / Deep rank，各阶段真实名次。"""
        evidence: dict = {
            "mfe_20": label.mfe_20,
            "mae_20": label.mae_20,
            "time_to_10": label.time_to_10,
        }
        if view.expert_ranks:
            # P0-11：[{expert, rank}] 按 rank ASC（旧版只存专家名列表）
            evidence["experts"] = [
                {"expert": expert, "rank": rank}
                for expert, rank in sorted(
                    view.expert_ranks.items(), key=lambda item: (item[1], item[0])
                )
            ]
        # P0-11：union_rank ≠ min(expert_rank)，用 Trace RECALL 行的
        # 真实 RRF 名次；无则不伪造
        if view.recall_rank is not None:
            evidence["recall_rank"] = view.recall_rank
        if view.pareto_front is not None:
            evidence["pareto_front"] = view.pareto_front
            evidence["protected"] = view.pareto_protected
        if view.machine_rank is not None:
            evidence["machine_rank"] = view.machine_rank
        if view.deep_rank is not None:
            evidence["deep_rank"] = view.deep_rank
        return evidence
