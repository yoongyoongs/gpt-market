"""L3 Feature Enrichment（设计 §2.1 L3）。

第一版数据面：全市场特征行（日K+周K extras）与证据视图在 Stage 0
已对全体候选算好，L3 只做「Union 命中 → 完整视图装配 + 覆盖标记」，
并显式暴露 60m 缺口（Machine Top120 后由 Deep 阶段拉取补评）。
"""

from __future__ import annotations

from app.v3.candidate_engine.experts.evidence_experts import (
    FINANCE_REPORT,
    PREDICT_REPORTS,
)
from app.v3.domain.candidate_engine import (
    EnrichedCandidate,
    EnrichmentCoverage,
    EnrichmentResult,
    RecallUnionResult,
)
from app.v3.domain.candidate_engine import ExpertInput

__all__ = ["FeatureEnrichmentService"]

_FQ_REPORTS = (FINANCE_REPORT, *PREDICT_REPORTS)


class FeatureEnrichmentService:
    """装配 Union 命中候选的完整特征/证据视图。"""

    def execute(
        self,
        union: RecallUnionResult,
        stocks_by_id: dict,
    ) -> EnrichmentResult:
        """stocks_by_id: {security_id: ExpertInput}（Stage 0 全市场构建）。"""
        candidates: list[EnrichedCandidate] = []
        missing = 0
        for entry in union.entries:
            stock = stocks_by_id.get(entry.security_id)
            if stock is None:
                missing += 1
                continue
            candidates.append(
                EnrichedCandidate(
                    entry=entry,
                    feature=stock.feature,
                    evidence_count=len(stock.evidence),
                    coverage=self._coverage(stock),
                )
            )
        return EnrichmentResult(candidates=tuple(candidates), missing_input_count=missing)

    @staticmethod
    def _coverage(stock: ExpertInput) -> EnrichmentCoverage:
        features = stock.feature.features
        fundamental = any(
            item.record.normalized_payload.get("report_name") in _FQ_REPORTS
            for item in stock.evidence
        )
        catalyst = any(
            item.record.evidence_type.value == "OFFICIAL_DISCLOSURE"
            for item in stock.evidence
        )
        return EnrichmentCoverage(
            daily_kline=bool(features),
            weekly_kline=features.get("weekly_slope_8w") is not None,
            fundamental_evidence=fundamental,
            catalyst_evidence=catalyst,
            minute_60=False,
        )
