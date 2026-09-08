"""Step 10-12：RR / Penalty / SoftOpportunity / Machine Rank / Deep Rank 测试。

覆盖：RR 锚点插值边界、Penalty 六规则与 -30 封顶、SoftOpp 八维合成
与 NonChase 代理、Machine 公式精确值与 Front 映射、Deep 趋势冲突
（§22.2）与 missing 降权、TopN 截断。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.v3.candidate_engine.experts.evidence_experts import FINANCE_REPORT
from app.v3.candidate_engine.deep_rank import WEIGHTS as DEEP_WEIGHTS
from app.v3.candidate_engine.deep_rank import DeepRankService
from app.v3.candidate_engine.machine_rank import MachineRankConfig, MachineRankService
from app.v3.candidate_engine.machine_rank import pareto_quality
from app.v3.candidate_engine.penalty import PenaltyEngine
from app.v3.candidate_engine.risk_reward import RiskRewardService, risk_reward_score
from app.v3.candidate_engine.soft_opportunity import SoftOpportunityService
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
    values["features"] = features
    return ExpertFeatureView(security_id=uuid4(), code="000001", **values)


def _stock(**features) -> ExpertInput:
    return ExpertInput(feature=_view(**features))


def _finance_evidence(period: str, revenue: float, profit: float) -> SecurityEvidenceView:
    record = NormalizedEvidence.build(
        raw_document_id=uuid4(),
        evidence_type=EvidenceType.FACT,
        source_type=EvidenceSourceType.OFFICIAL,
        source_priority=1,
        subject_type="SECURITY",
        subject_id="000001",
        claim_key="claim",
        source="fixture",
        payload={},
        normalized_payload={
            "report_name": FINANCE_REPORT,
            "report_period": period,
            "values": {
                "TOTALOPERATEREVE": revenue,
                "PARENTNETPROFIT": profit,
            },
        },
        event_time=NOW - timedelta(days=3),
        publish_time=NOW - timedelta(days=3),
        fetch_time=NOW,
        known_at=NOW,
        confidence=1.0,
        relevance=1.0,
        parser_version="test-v1",
    )
    return SecurityEvidenceView(
        security_id=uuid4(), record=record, effective_relevance=1.0
    )


# ---------- §17 Risk / Reward ----------


class TestRiskRewardScore:
    def test_anchors(self):
        assert risk_reward_score(0.0) == 10.0
        assert risk_reward_score(0.8) == 30.0
        assert risk_reward_score(1.2) == 55.0
        assert risk_reward_score(1.8) == 80.0
        assert risk_reward_score(2.5) == 100.0
        assert risk_reward_score(4.0) == 95.0

    def test_interpolation_midpoints(self):
        assert risk_reward_score(0.4) == pytest.approx(20.0)
        assert risk_reward_score(1.0) == pytest.approx(42.5)
        assert risk_reward_score(2.15) == pytest.approx(90.0)

    def test_beyond_and_below(self):
        assert risk_reward_score(-1.0) == 10.0
        assert risk_reward_score(8.0) == 95.0
        assert risk_reward_score(float("nan")) == 0.0


class TestRiskRewardService:
    def test_full_view(self):
        assessment = RiskRewardService().evaluate(
            _view(distance_60d_low=0.10, distance_60d_high=-0.20)
        )
        # 支撑 = 10/1.1 ≈ 9.0909，压力 = 10/0.8 = 12.5
        assert assessment.support == pytest.approx(9.0909, abs=1e-3)
        assert assessment.resistance == pytest.approx(12.5)
        assert assessment.downside == pytest.approx(0.10 / 1.10, abs=1e-4)
        # P0-06：upside = -dist_high/(1+dist_high) = 0.20/0.80 = 0.25
        assert assessment.upside == pytest.approx(0.25, abs=1e-4)
        assert assessment.rr == pytest.approx(2.75, abs=1e-2)
        # 锚点插值 (2.5,100)→(4.0,95)：t=0.25/1.5 → 100-5*0.16667
        assert assessment.score == pytest.approx(99.1667, abs=1e-3)
        assert assessment.confidence == 1.0

    def test_upside_exact_consistency_close10_high12_low95(self):
        """任务书 P0-06 §8.3 一致性：close=10/high=12/low=9.5
        → upside=20%、downside=5%、RR=4（不接受 16.67% 近似）。"""
        assessment = RiskRewardService().evaluate(
            _view(
                distance_60d_low=10.0 / 9.5 - 1.0,
                distance_60d_high=10.0 / 12.0 - 1.0,
            )
        )
        assert assessment.support == pytest.approx(9.5, abs=1e-9)
        assert assessment.resistance == pytest.approx(12.0, abs=1e-9)
        assert assessment.upside == pytest.approx(0.20, abs=1e-9)
        assert assessment.downside == pytest.approx(0.05, abs=1e-9)
        assert assessment.rr == pytest.approx(4.0, abs=1e-9)
        # rr=4.0 命中 (4.0, 95) 锚点
        assert assessment.score == pytest.approx(95.0, abs=1e-6)

    def test_support_too_close_low_confidence(self):
        assessment = RiskRewardService().evaluate(
            _view(distance_60d_low=0.002, distance_60d_high=-0.10)
        )
        assert assessment.confidence == 0.5

    def test_missing_features_zero(self):
        assessment = RiskRewardService().evaluate(_view())
        assert assessment.score == 0.0
        assert assessment.support is None


# ---------- §20 Penalty Engine ----------


class TestPenaltyEngine:
    def test_no_rules_zero(self):
        assert PenaltyEngine().evaluate(_stock()).total == 0.0

    def test_overheat_tiers(self):
        assert PenaltyEngine().evaluate(_stock(return_20d=0.30)).total == -5.0
        assert PenaltyEngine().evaluate(_stock(return_20d=0.45)).total == -12.0

    def test_stretched_from_ma20(self):
        assert PenaltyEngine().evaluate(_stock(price_ma20_distance=0.20)).total == -8.0

    def test_volume_acceleration_tiers(self):
        assert PenaltyEngine().evaluate(_stock(volume_spike=3.0)).total == -5.0
        assert PenaltyEngine().evaluate(
            _stock(volume_spike=3.0, consecutive_up_days=6)
        ).total == -12.0

    def test_weekly_severe_decline(self):
        assert PenaltyEngine().evaluate(_stock(weekly_slope_8w=-0.05)).total == -5.0

    def test_fundamental_worsening(self):
        stock = ExpertInput(
            feature=_view(),
            evidence=(
                _finance_evidence("2025-12-31", 0.8e9, 0.4e9),
                _finance_evidence("2024-12-31", 1.0e9, 0.5e9),
            ),
        )
        assessment = PenaltyEngine().evaluate(stock)
        assert assessment.total == -10.0
        assert assessment.items[0][0] == "fundamental_worsening"

    def test_nearby_resistance_tiers(self):
        assert PenaltyEngine().evaluate(_stock(distance_60d_high=-0.005)).total == -10.0
        assert PenaltyEngine().evaluate(_stock(distance_60d_high=-0.02)).total == -5.0

    def test_cap_minus_30(self):
        stock = _stock(
            return_20d=0.45,          # 12
            price_ma20_distance=0.20,  # 8
            volume_spike=3.0,          # 5
            weekly_slope_8w=-0.05,     # 5
        )
        assert PenaltyEngine().evaluate(stock).total == -30.0


# ---------- §19 SoftOpportunity ----------


FULL_EXPERT_SCORES = {"LP": 80.0, "BT": 70.0, "RV": 60.0, "AC": 50.0, "FQ": 40.0, "CAT": 30.0}


class TestSoftOpportunity:
    def test_full_dimensions_exact_value(self):
        rr = RiskRewardService().evaluate(_view(distance_60d_low=0.10, distance_60d_high=-0.20))
        result = SoftOpportunityService().evaluate(_stock(), FULL_EXPERT_SCORES, rr)
        # raw = 0.8*20 + 0.65*20 + 0.5*15 + 0.7*10 + 0(nonchase) + 0.4*8 + 0.3*7 + rr/10
        # P0-05 归一：value = raw / 有效权重90 * 100
        raw = 16 + 13 + 7.5 + 7 + 3.2 + 2.1 + rr.score / 10.0
        assert result.value == pytest.approx(raw / 90.0 * 100.0, abs=1e-3)
        # nonchase 缺（无 return_20d/连涨/spike）→ 有效权重 90/100
        assert result.confidence == pytest.approx(0.9)
        assert result.net_value == pytest.approx(result.value, abs=1e-3)

    def test_missing_dimensions_lower_confidence(self):
        rr = RiskRewardService().evaluate(_view())
        result = SoftOpportunityService().evaluate(_stock(), {}, rr)
        # position20+transition20+accumulation15+bottoming10+fundamental8+catalyst7
        # = 80 有效 / 100 总（nonchase/risk_reward 由数据决定，这里也缺）
        assert result.confidence < 0.2
        assert result.value == 0.0

    def test_penalty_integrated_in_net(self):
        engine = PenaltyEngine()
        rr = RiskRewardService().evaluate(_view(distance_60d_low=0.10, distance_60d_high=-0.20))
        service = SoftOpportunityService(penalty_engine=engine)
        result = service.evaluate(_stock(return_20d=0.45), FULL_EXPERT_SCORES, rr)
        assert result.penalty.total == -12.0
        assert result.net_value == pytest.approx(result.value - 12.0, abs=1e-3)

    def test_nonchase_all_proxies_calm(self):
        rr = RiskRewardService().evaluate(_view())
        stock = _stock(return_20d=0.0, consecutive_up_days=0, volume_spike=1.0)
        result = SoftOpportunityService().evaluate(stock, {}, rr)
        # 三个子代理全部满分 → nonchase 维 1.0；P0-05 归一：
        # 仅 nonchase 生效（有效权重 10/100）→ value=100，confidence=0.1
        assert result.value == pytest.approx(100.0, abs=1e-3)
        assert result.confidence == pytest.approx(0.1, abs=1e-3)


# ---------- §21 Machine Rank ----------


def _pool_item(rrf: float, soft: float, front: int = 1, crowding: float = 0.0) -> dict:
    return {
        "security_id": uuid4(),
        "code": f"{int(rrf):06d}",
        "rrf_norm": rrf,
        "soft_net": soft,
        "front": front,
        "crowding": crowding,
    }


class TestMachineRank:
    def test_formula_exact(self):
        result = MachineRankService().execute([_pool_item(80.0, 70.0, front=1)])
        # 0.35*80 + 0.50*70 + 0.15*100 = 78
        assert result.entries[0].machine_score == pytest.approx(78.0)
        assert result.entries[0].rank == 1
        assert result.entries[0].selected is True

    def test_front_quality_mapping(self):
        assert pareto_quality(1, 0.0) == 100.0
        assert pareto_quality(2, 0.0) == 85.0
        assert pareto_quality(3, 0.0) == 70.0
        assert pareto_quality(4, 0.0) == 55.0
        assert pareto_quality(5, 0.0) == 40.0
        assert pareto_quality(6, 0.0) == 30.0
        assert pareto_quality(9, 0.0) == 0.0

    def test_crowding_boundary_bonus(self):
        assert pareto_quality(2, 1e9) == 90.0
        assert pareto_quality(1, 1e9) == 100.0  # 封顶

    def test_config_bounds(self):
        with pytest.raises(ValueError):
            MachineRankConfig(top_n=99)
        with pytest.raises(ValueError):
            MachineRankConfig(top_n=151)

    def test_top_n_selection(self):
        pool = [_pool_item(float(i), 50.0) for i in range(130)]
        result = MachineRankService().execute(pool)
        assert result.evaluated_count == 130
        assert result.top_n == 120
        assert len(result.entries) == 130
        selected = [entry for entry in result.entries if entry.selected]
        assert len(selected) == 120
        assert result.entries[0].rank == 1
        assert result.entries[-1].selected is False

    def test_sort_tie_by_code(self):
        a = _pool_item(80.0, 70.0)
        b = _pool_item(80.0, 70.0)
        result = MachineRankService().execute([b, a])
        codes = [entry.code for entry in result.entries]
        assert codes == sorted(codes)


# ---------- §22 Deep Rank ----------


def _deep_item(**extra) -> dict:
    base = {
        "security_id": uuid4(),
        "code": f"{uuid4().int % 1000000:06d}",
        "machine_score": 80.0,
    }
    base.update(extra)
    return base


class TestDeepRank:
    def test_weights_sum_100(self):
        assert sum(DEEP_WEIGHTS.values()) == 100.0

    def test_machine_only_low_confidence(self):
        result = DeepRankService().execute([_deep_item()])
        entry = result.entries[0]
        # 60m/市场/行业/周K/日K/RR 全 missing → 有效权重 45/100；
        # P0-05 归一：machine 0.8 为唯一有效维 → deep_score=80
        assert entry.confidence == pytest.approx(0.45)
        assert entry.deep_score == pytest.approx(80.0, abs=1e-3)

    def test_full_known_dimensions(self):
        item = _deep_item(
            weekly_state="UP",
            daily_state="UP",
            rr_score=80.0,
        )
        result = DeepRankService().execute([item])
        entry = result.entries[0]
        # raw = machine 0.8*45=36 + weekly 0.9*15=13.5 + daily 0.9*15=13.5
        #       + rr 0.8*5=4 = 67；P0-05 归一：67/有效权重80*100 = 83.75
        assert entry.deep_score == pytest.approx(67.0 / 80.0 * 100.0, abs=1e-3)
        assert entry.confidence == pytest.approx(0.80)  # (45+15+15+5)/100
        assert entry.trend_conflict is False

    def test_trend_conflict_capped(self):
        conflict_item = _deep_item(
            weekly_state="DOWN",
            daily_state="UP",
            multi_state="WEEKLY_DOWN_DAILY_BOUNCE",
        )
        result = DeepRankService().execute([conflict_item])
        entry = result.entries[0]
        assert entry.trend_conflict is True
        assert "trend_conflict_no_reversal_evidence" in entry.reasons
        # 日K 被压到 40：raw = machine 36 + weekly 4.5 + daily 6 = 46.5，
        # 有效权重 75 → P0-05 归一 46.5/75*100 = 62.0
        assert entry.deep_score == pytest.approx(46.5 / 75.0 * 100.0, abs=1e-3)

    def test_reversal_evidence_waives_conflict(self):
        item = _deep_item(
            weekly_state="DOWN",
            daily_state="UP",
            multi_state="WEEKLY_DOWN_DAILY_BOUNCE",
            weekly_decline_deceleration=0.02,
        )
        result = DeepRankService().execute([item])
        assert result.entries[0].trend_conflict is False

    def test_slope_proxy_fallback(self):
        item = _deep_item(weekly_slope_8w=0.05)  # 饱和 → 100 分代理
        result = DeepRankService().execute([item])
        # raw = machine 36 + weekly 15 = 51，有效权重 60 → P0-05 归一 85.0
        assert result.entries[0].deep_score == pytest.approx(51.0 / 60.0 * 100.0, abs=1e-3)

    def test_top60_truncation(self):
        pool = [_deep_item(machine_score=float(90 - i)) for i in range(70)]
        result = DeepRankService().execute(pool)
        assert result.evaluated_count == 70
        assert result.top_n == 60
        assert len(result.entries) == 60
        assert result.entries[0].deep_score >= result.entries[-1].deep_score
        assert [entry.rank for entry in result.entries] == list(range(1, 61))

    def test_state_scores(self):
        from app.v3.candidate_engine.deep_rank import _state_score, _slope_proxy

        assert _state_score("UP", None) == 90.0
        assert _state_score("FLAT", None) == 60.0
        assert _state_score("DOWN", None) == 30.0
        assert _state_score(None, 55.0) == 55.0
        assert _state_score(None, None) is None
        assert _slope_proxy(0.0) == 60.0
        assert _slope_proxy(-0.05) == 20.0
        assert _slope_proxy(0.10) == 100.0
