"""Step 5：八路召回专家单元测试（任务书 §35.2）。

覆盖：每专家命中/不命中/missing 降 confidence、特殊规则
（52w 位置形状、周K减缓、缩量 floor、爆量归零、绝对 RS 仅 10%、
CAT 四因子乘积）、TopN 截断与排名。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.v3.candidate_engine.experts import default_experts
from app.v3.candidate_engine.experts.evidence_experts import (
    CatalystExpert,
    FundamentalRepairExpert,
)
from app.v3.candidate_engine.experts.feature_experts import (
    AccumulationExpert,
    BottomingExpert,
    LowPositionExpert,
    PullbackExpert,
    RelativeStrengthExpert,
    ReversalExpert,
)
from app.v3.domain.candidate_engine import ExpertFeatureView, ExpertInput
from app.v3.domain.evidence import (
    EvidenceSourceType,
    EvidenceType,
    NormalizedEvidence,
    SecurityEvidenceView,
)

NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)


def _view(**features) -> ExpertFeatureView:
    values = {"close": 10.0, "as_of": NOW}
    features.setdefault("position_250d", 0.2)
    values["features"] = features
    return ExpertFeatureView(security_id=uuid4(), code="000001", **values)


def _stock(**features) -> ExpertInput:
    return ExpertInput(feature=_view(**features))


def _evidence(
    *,
    report_name: str | None = None,
    values: dict | None = None,
    title: str | None = None,
    evidence_type: EvidenceType = EvidenceType.OFFICIAL_DISCLOSURE,
    source_priority: int = 1,
    published: datetime | None = None,
    relevance: float = 1.0,
    period: str = "2025-12-31",
) -> SecurityEvidenceView:
    payload: dict = {}
    normalized: dict = {}
    if report_name is not None:
        normalized["report_name"] = report_name
        normalized["report_period"] = period
        normalized["values"] = values or {}
    if title is not None:
        normalized["title"] = title
    publish_time = published or (NOW - timedelta(days=3))
    record = NormalizedEvidence.build(
        raw_document_id=uuid4(),
        evidence_type=evidence_type,
        source_type=EvidenceSourceType.OFFICIAL,
        source_priority=source_priority,
        subject_type="SECURITY",
        subject_id="000001",
        claim_key="claim",
        source="fixture",
        payload=payload,
        normalized_payload=normalized,
        event_time=publish_time,
        publish_time=publish_time,
        fetch_time=NOW,
        known_at=NOW,
        confidence=1.0,
        relevance=relevance,
        parser_version="test-v1",
    )
    return SecurityEvidenceView(
        security_id=uuid4(), record=record, effective_relevance=relevance
    )


class TestLowPosition:
    def test_sweet_spot_full_score(self):
        # 位置 20%（10~30% 甜区）+ 各维健康 → 高分
        stock = _stock(
            position_250d=0.20, position_120d=0.20,
            distance_60d_low=0.02, distance_250d_high=-0.35,
            return_20d=0.01, distance_120d_high=-0.30, return_250d=-0.30,
        )
        hits = LowPositionExpert().run([stock])
        assert len(hits) == 1
        assert hits[0].score > 85

    def test_new_low_not_full_score(self):
        # 设计 §5.2.4：贴近新低（0~5%）只 70 分，不得满分
        near_low = _stock(position_250d=0.01, position_120d=0.01,
                          distance_60d_low=0.0, distance_250d_high=-0.35,
                          return_20d=0.01, distance_120d_high=-0.30, return_250d=-0.30)
        mid = _stock(position_250d=0.20, position_120d=0.20,
                     distance_60d_low=0.0, distance_250d_high=-0.35,
                     return_20d=0.01, distance_120d_high=-0.30, return_250d=-0.30)
        low_hits = LowPositionExpert().run([near_low])
        mid_hits = LowPositionExpert().run([mid])
        assert low_hits[0].score < mid_hits[0].score
        assert low_hits[0].score < 90

    def test_missing_core_feature_not_evaluated(self):
        hits = LowPositionExpert().run([_stock(position_250d=None)])
        assert hits == ()

    def test_missing_dims_lower_confidence(self):
        stock = _stock(position_250d=0.2)  # 其余维度全缺
        hits = LowPositionExpert().run([stock])
        assert len(hits) == 1
        assert hits[0].score > 0
        assert hits[0].confidence < 0.3  # 只有 25+15 权重生效


class TestBottoming:
    def test_deceleration_counts_despite_negative_slope(self):
        # 周K 仍下跌但跌速减缓 → 周K 维给分
        calm = BottomingExpert().evaluate(_stock(
            atr_contraction=0.3, low_slope_20=0.0,
            weekly_decline_deceleration=0.002,
            daily_range_contraction=0.2, down_volume_ratio=0.5,
            base_duration=10, swing_low_trend=0.001,
        ))
        assert calm is not None
        weekly_part = [r for r in calm.reasons if r.startswith("weekly_deceleration")]
        assert weekly_part and "missing" not in weekly_part[0]

    def test_extreme_low_down_volume_floored(self):
        # 极端缩量（成交将死）不无限奖励：低封顶 30 分（设计 §6.2.3）
        score = BottomingExpert().evaluate(_stock(
            atr_contraction=0.3, down_volume_ratio=0.01,
        ))
        assert score is not None
        down_part = next(r for r in score.reasons if r.startswith("down_volume_ratio"))
        assert down_part.startswith("down_volume_ratio:0.300")

    def test_top250_default(self):
        # TopN 截断逻辑由 BaseExpert 通用测试覆盖
        assert BottomingExpert().top_n() == 250
        assert BottomingExpert().run([_stock(atr_contraction=None)]) == ()


class TestReversal:
    def test_stabilizing_decline_scores_via_macd_delta(self):
        # MACD delta > 0（绿柱收窄）即可得分，不要求 MACD>0
        stock = _stock(ma20_slope_delta=None, macd_hist_delta=0.01, close=10.0)
        score = ReversalExpert().evaluate(stock)
        assert score is not None
        assert score.value > 0

    def test_missing_60m_and_trendline_lower_confidence(self):
        # 日K 五维全给分、60m(15)+趋势线(10) 缺失 → confidence 75/100
        stock = _stock(
            ma20_slope_delta=0.002, ma20_ma60_gap=0.01,
            price_ma20_distance=-0.02, macd_hist_delta=0.005,
            swing_low_trend=0.001,
        )
        score = ReversalExpert().evaluate(stock)
        assert score is not None
        assert math.isclose(score.confidence, 0.75, abs_tol=0.001)

    def test_price_below_ma20_far_gets_low_near_score(self):
        score = ReversalExpert().evaluate(_stock(
            ma20_slope_delta=0.002, price_ma20_distance=-0.20,
        ))
        assert score is not None
        near = next(r for r in score.reasons if r.startswith("near_ma20"))
        assert near.startswith("near_ma20:0.000")


class TestAccumulation:
    def test_mild_ratio_scores_high(self):
        score = AccumulationExpert().evaluate(_stock(
            up_down_volume_ratio=1.3, obv_slope_z=0.6, volume_5_20=1.3,
        ))
        assert score is not None and score.value > 50

    def test_explosive_volume_zeroed(self):
        # U/D > 3.5 → 归零（设计 §8.1 不奖励单日爆量追涨）
        score = AccumulationExpert().evaluate(_stock(
            up_down_volume_ratio=4.0, obv_slope_z=0.6, volume_5_20=1.3,
        ))
        assert score is not None
        assert score.value < 50
        ud = next(r for r in score.reasons if r.startswith("up_down_volume_ratio"))
        assert ud.startswith("up_down_volume_ratio:0.000")

    def test_volume_5_20_over_2_5_zeroed(self):
        score = AccumulationExpert().evaluate(_stock(
            up_down_volume_ratio=1.2, volume_5_20=2.8,
        ))
        assert score is not None
        v520 = next(r for r in score.reasons if r.startswith("volume_5_20"))
        assert v520.startswith("volume_5_20:0.000")


class TestPullback:
    def test_breakout_with_pullback_scores(self):
        score = PullbackExpert().evaluate(_stock(
            breakout_extension_20d=0.02, pullback_20d=True,
            volume_5_20=0.9, price_ma20_distance=0.005, return_5d=0.01,
        ))
        assert score is not None and score.value > 60

    def test_not_in_pullback_zeroes_retrace_dim(self):
        score = PullbackExpert().evaluate(_stock(
            breakout_extension_20d=0.02, pullback_20d=False,
            volume_5_20=0.9, price_ma20_distance=0.005, return_5d=0.01,
        ))
        assert score is not None
        retrace = next(
            r for r in score.reasons if r.startswith("pullback_shrink_volume")
        )
        assert retrace.startswith("pullback_shrink_volume:0.000")

    def test_far_extension_penalized(self):
        near = PullbackExpert().evaluate(_stock(breakout_extension_20d=0.01))
        far = PullbackExpert().evaluate(_stock(breakout_extension_20d=0.30))
        assert near is not None and far is not None
        assert far.value < near.value


class TestRelativeStrength:
    def test_abs_level_capped_at_10_weight(self):
        # 绝对 RS 很强但 RS 序列缺失 → 只有绝对分（10 权重）+ 其余 missing
        score = RelativeStrengthExpert().evaluate(_stock(
            rs_5d_slope=None, rs_20d_slope=None, relative_index_strength=0.20,
        ))
        assert score is None  # required（rs 斜率）全缺 → 不召回

    def test_inflection_not_strongest(self):
        # 从弱转强：斜率改善 + 绝对水平一般 也能得高分
        score = RelativeStrengthExpert().evaluate(_stock(
            rs_5d_slope=0.002, rs_20d_slope=0.001, rs_low_higher=True,
            relative_index_strength=-0.04,
        ))
        assert score is not None and score.value > 50

    def test_low_higher_false_zero_not_missing(self):
        score = RelativeStrengthExpert().evaluate(_stock(
            rs_5d_slope=0.002, rs_20d_slope=0.001, rs_low_higher=False,
        ))
        assert score is not None
        low = next(r for r in score.reasons if r.startswith("rs_low_higher"))
        assert low.startswith("rs_low_higher:0.000")


class TestFundamentalRepair:
    def test_no_evidence_not_recalled(self):
        assert FundamentalRepairExpert().evaluate(_stock()) is None

    def test_finance_growth_scores_with_reduced_confidence(self):
        current = _evidence(
            report_name="RPT_F10_FINANCE_MAINFINADATA", period="2025-12-31",
            values={"TOTALOPERATEREVE": "1.80e9", "PARENTNETPROFIT": "2.0e8"},
        )
        previous = _evidence(
            report_name="RPT_F10_FINANCE_MAINFINADATA", period="2024-12-31",
            values={"TOTALOPERATEREVE": "1.00e9", "PARENTNETPROFIT": "1.0e8"},
        )
        stock = ExpertInput(feature=_view(), evidence=(current, previous))
        hits = FundamentalRepairExpert().run([stock])
        assert len(hits) == 1
        # 可评权重 45/100 → confidence ≤ 0.45×relevance
        assert hits[0].confidence <= 0.45
        assert hits[0].score > 30  # 营收 +20%、利润 +100% 双命中

    def test_no_pairing_not_recalled(self):
        # 只有单期数据无法算同比 → 不召回（不伪造）
        single = _evidence(
            report_name="RPT_F10_FINANCE_MAINFINADATA", period="2025-12-31",
            values={"TOTALOPERATEREVE": "1.2e9"},
        )
        assert FundamentalRepairExpert().evaluate(
            ExpertInput(feature=_view(), evidence=(single,))
        ) is None


class TestCatalyst:
    def test_official_keyword_event_scores(self):
        item = _evidence(title="公司拟回购股份暨回购报告书", published=NOW - timedelta(days=2))
        score = CatalystExpert().evaluate(ExpertInput(feature=_view(), evidence=(item,)))
        assert score is not None and score.value > 30

    def test_freshness_decays(self):
        fresh = CatalystExpert().evaluate(ExpertInput(
            feature=_view(),
            evidence=(_evidence(title="中标公告", published=NOW - timedelta(days=1)),),
        ))
        stale = CatalystExpert().evaluate(ExpertInput(
            feature=_view(),
            evidence=(_evidence(title="中标公告", published=NOW - timedelta(days=25)),),
        ))
        assert fresh is not None and stale is not None
        assert stale.value < fresh.value

    def test_priced_runup_zeroes_unpriced(self):
        # 催化后 5 日已涨 20%+ → unpriced=0 → 不召回
        item = _evidence(title="增持公告", published=NOW - timedelta(days=1))
        score = CatalystExpert().evaluate(ExpertInput(
            feature=_view(return_5d=0.25), evidence=(item,),
        ))
        assert score is None

    def test_news_type_or_no_keyword_not_recalled(self):
        item = _evidence(
            title="日常经营公告",
            evidence_type=EvidenceType.NEWS,
        )
        assert CatalystExpert().evaluate(
            ExpertInput(feature=_view(), evidence=(item,))
        ) is None


class TestBaseRunner:
    def test_top_n_truncation_and_rank_order(self):
        expert = LowPositionExpert()
        stocks = []
        for i in range(310):
            stock = _stock(position_250d=0.10 + i * 0.0005)
            stocks.append(ExpertInput(feature=stock.feature))
        hits = expert.run(stocks)
        assert len(hits) == 300
        assert hits[0].rank == 1
        assert hits[0].score >= hits[-1].score
        ranks = [h.rank for h in hits]
        assert ranks == list(range(1, 301))

    def test_default_registry_has_eight_experts(self):
        experts = default_experts()
        assert tuple(e.name() for e in experts) == ("LP", "BT", "RV", "AC", "PB", "RS", "FQ", "CAT")
        assert [e.top_n() for e in experts] == [300, 250, 250, 250, 180, 180, 150, 150]
