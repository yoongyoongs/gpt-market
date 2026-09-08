"""Step 6-7：Recall Union + RRF + Feature Enrichment 测试（任务书 §35）。

核心契约：
- 不设「命中最少专家数」（设计 §13.2）：单路 #1 必须保留且高分；
- RRF 公式与 K=60 常数、归一化 0~100、退化区间；
- L3 装配完整视图，60m 缺口显式标记，missing 输入可观测。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.v3.candidate_engine.enrichment import FeatureEnrichmentService
from app.v3.candidate_engine.union import DEFAULT_RRF_K, RecallUnionService
from app.v3.domain.candidate_engine import (
    ExpertFeatureView,
    ExpertHit,
    ExpertInput,
)

NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)


def _hit(expert: str, code: str, rank: int, score: float = 80.0,
         security_id=None) -> ExpertHit:
    return ExpertHit(
        expert=expert, security_id=security_id or uuid4(), code=code,
        score=score, rank=rank,
    )


class TestRecallUnionRRF:
    def test_multi_rank_beats_single_rank(self):
        """同股命中两路 > 单路（设计 §14：多路中排 > 单路后排）。"""
        shared = uuid4()
        hit_a = _hit("LP", "000001", 1, security_id=shared)
        hit_b = _hit("BT", "000001", 1, security_id=shared)
        hit_c = _hit("RV", "600000", 1)
        result = RecallUnionService().execute({
            "LP": (hit_a,), "BT": (hit_b,), "RV": (hit_c,),
        })
        assert result.union_count == 2
        first = result.entries[0]
        assert first.security_id == shared
        assert math_isclose(first.rrf_raw, 2.0 / (DEFAULT_RRF_K + 1))
        assert result.entries[1].rrf_raw < first.rrf_raw

    def test_single_expert_first_keeps_high_rank(self):
        """单路 #1 的高 RRF 不被多路低排名淹没（1/61 > 各路 1/62 组合？

        反例验证：单路 rank1 = 0.0164，两路 rank500 级不可能出现在
        TopN 内；此处验证单路 #1 > 单路 #2。）
        """
        first = _hit("LP", "000001", 1)
        second = _hit("LP", "000002", 2)
        result = RecallUnionService().execute({"LP": (first, second)})
        assert result.entries[0].security_id == first.security_id
        assert math_isclose(
            result.entries[0].rrf_raw, 1.0 / (DEFAULT_RRF_K + 1)
        )
        assert math_isclose(
            result.entries[1].rrf_raw, 1.0 / (DEFAULT_RRF_K + 2)
        )

    def test_no_minimum_expert_count(self):
        """只命中 FQ 一路也保留（设计 §13.2）。"""
        only_fq = _hit("FQ", "300001", 3)
        result = RecallUnionService().execute({"FQ": (only_fq,)})
        assert result.union_count == 1
        assert result.entries[0].expert_names == ("FQ",)

    def test_normalization_bounds_and_degenerate(self):
        hits = (_hit("LP", "000001", 1), _hit("LP", "000002", 2), _hit("LP", "000003", 3))
        result = RecallUnionService().execute({"LP": hits})
        norms = [entry.rrf_norm for entry in result.entries]
        assert max(norms) == 100.0
        assert min(norms) == 0.0
        # 退化：所有 rrf 相同 → 全 100（不得除零）
        degenerate_id = uuid4()
        same = (_hit("LP", "000001", 1, security_id=degenerate_id), _hit("BT", "000001", 1, security_id=degenerate_id))
        degenerate = RecallUnionService().execute({"LP": (same[0],), "BT": (same[1],)})
        assert all(entry.rrf_norm == 100.0 for entry in degenerate.entries)

    def test_expert_weight_override(self):
        hit = _hit("LP", "000001", 1)
        weighted = RecallUnionService(weights={"LP": 2.0}).execute({"LP": (hit,)})
        plain = RecallUnionService().execute({"LP": (hit,)})
        assert math_isclose(weighted.entries[0].rrf_raw, 2.0 / (DEFAULT_RRF_K + 1))
        assert math_isclose(plain.entries[0].rrf_raw, 1.0 / (DEFAULT_RRF_K + 1))

    def test_empty_and_invalid(self):
        empty = RecallUnionService().execute({})
        assert empty.union_count == 0 and empty.entries == ()
        try:
            RecallUnionService(k=0)
        except ValueError:
            pass
        else:
            raise AssertionError("k=0 must raise")

    def test_evaluated_count_preserved(self):
        hit = _hit("LP", "000001", 1)
        result = RecallUnionService().execute({"LP": (hit,)}, evaluated_count=5000)
        assert result.evaluated_count == 5000

    def test_rank_sequence_dense(self):
        hits_a = (_hit("LP", "000001", 1), _hit("LP", "000002", 2))
        hits_b = (_hit("BT", "000003", 1),)
        result = RecallUnionService().execute({"LP": hits_a, "BT": hits_b})
        assert [entry.union_rank for entry in result.entries] == [1, 2, 3]


def math_isclose(a: float, b: float) -> bool:
    return abs(a - b) < 1e-12


class TestFeatureEnrichment:
    def _view(self, **features) -> ExpertFeatureView:
        payload = {"close": 10.0, "as_of": NOW, "features": dict(features)}
        return ExpertFeatureView(security_id=uuid4(), code="000001", **payload)

    def test_assembles_full_view_and_coverage(self):
        from tests.v3.test_candidate_experts import _evidence

        finance = _evidence(
            report_name="RPT_F10_FINANCE_MAINFINADATA", period="2025-12-31",
            values={"TOTALOPERATEREVE": "100"},
        )
        catalyst = _evidence(title="中标公告")
        union = RecallUnionService().execute({
            "FQ": (_hit("FQ", "000001", 1, score=50.0),),
        })
        hit = union.entries[0]
        stock = ExpertInput(
            feature=ExpertFeatureView(
                security_id=hit.security_id, code="000001",
                close=10.0, as_of=NOW,
                features={"bar_count": 250, "weekly_slope_8w": -0.01},
            ),
            evidence=(finance, catalyst),
        )
        result = FeatureEnrichmentService().execute(union, {hit.security_id: stock})
        assert result.missing_input_count == 0
        candidate = result.candidates[0]
        assert candidate.entry.rrf_norm == 100.0
        assert candidate.evidence_count == 2
        assert candidate.coverage.daily_kline is True
        assert candidate.coverage.weekly_kline is True
        assert candidate.coverage.fundamental_evidence is True
        assert candidate.coverage.catalyst_evidence is True
        assert candidate.coverage.minute_60 is False  # Deep 阶段补

    def test_missing_input_counted_not_silent(self):
        view = self._view()
        hit = _hit("LP", "000001", 1)
        union = RecallUnionService().execute({"LP": (hit,)})
        result = FeatureEnrichmentService().execute(union, {})
        assert result.missing_input_count == 1
        assert result.candidates == ()

    def test_sparse_coverage_flags(self):
        hit = _hit("LP", "000001", 1)
        union = RecallUnionService().execute({"LP": (hit,)})
        stock = ExpertInput(
            feature=ExpertFeatureView(
                security_id=hit.security_id, code="000001",
                close=10.0, as_of=NOW, features={},
            ),
        )
        result = FeatureEnrichmentService().execute(union, {hit.security_id: stock})
        coverage = result.candidates[0].coverage
        assert coverage.daily_kline is False
        assert coverage.weekly_kline is False
        assert coverage.fundamental_evidence is False
        assert coverage.catalyst_evidence is False
        assert coverage.minute_60 is False
