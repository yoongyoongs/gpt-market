"""P1-06：RR 结构升级（任务书 §24，可选级）。

- StructureLevel 契约：每个支撑/压力带来源(type)/时间戳/置信
- evaluate(levels=...) 结构选择：invalidation 优先结构低点，
  不机械取 60 日最低点（§24）
- levels=None 默认 v1 路径零破坏；候选不足回退 v1 代理
- structure_levels_from_features / structure_levels_from_minute60 构建器
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.v3.candidate_engine.risk_reward import (
    RiskRewardService,
    structure_levels_from_features,
    structure_levels_from_minute60,
)
from app.v3.domain.candidate_engine import ExpertFeatureView, StructureLevel

NOW = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)


def _view(**features) -> ExpertFeatureView:
    return ExpertFeatureView(
        security_id=uuid4(), code="000001", close=10.0, as_of=NOW,
        features=features,
    )


PROXY_VIEW = _view(
    distance_60d_low=0.10,       # 60d low ≈ 9.0909
    distance_60d_high=-0.20,     # 60d high = 12.5
    distance_120d_high=-0.15,    # 120d high ≈ 11.7647
    close_ma20_distance=-0.02,   # MA20 ≈ 10.2041（close 下方 2% → 均线在上方）
)


class TestFeatureLevelsBuilder:
    def test_distances_invert_to_prices(self):
        levels = {level.type: level for level in structure_levels_from_features(PROXY_VIEW)}
        assert levels["LOW_60D"].price == pytest.approx(10.0 / 1.10, abs=1e-6)
        assert levels["HIGH_60D"].price == pytest.approx(12.5, abs=1e-6)
        assert levels["HIGH_120D"].price == pytest.approx(10.0 / 0.85, abs=1e-6)
        assert levels["MA20"].price == pytest.approx(10.0 / 0.98, abs=1e-6)

    def test_levels_carry_source_time_confidence(self):
        """任务书 §24：每个支撑/压力必须带 source 和 timestamp。"""
        by_type = {level.type: level for level in structure_levels_from_features(PROXY_VIEW)}
        assert by_type["MA20"].confidence == pytest.approx(0.6)
        assert by_type["LOW_60D"].confidence == pytest.approx(0.5)
        for level in by_type.values():
            assert level.as_of == NOW
            assert 0.0 < level.confidence <= 1.0

    def test_swing_prices_never_fabricated(self):
        """swing 价格不在特征行 → 不得伪造候选（missing ≠ 0）。"""
        types = {level.type for level in structure_levels_from_features(PROXY_VIEW)}
        assert not types & {"SWING_LOW_60M", "SWING_HIGH_60M", "PULLBACK_LOW"}

    def test_missing_features_skipped(self):
        view = _view(distance_60d_low=0.10)  # 其余全缺
        types = {level.type for level in structure_levels_from_features(view)}
        assert types == {"LOW_60D"}


class TestStructureSelection:
    def test_structured_low_preferred_over_mechanical_60d(self):
        """§24：invalidation 优先结构低点（SWING_LOW_60M），非机械 60 日低点。"""
        levels = (
            StructureLevel(price=9.0909, type="LOW_60D", as_of=NOW, confidence=0.5),
            StructureLevel(price=9.5, type="SWING_LOW_60M", as_of=NOW, confidence=0.8),
            StructureLevel(price=11.0, type="SWING_HIGH_60M", as_of=NOW, confidence=0.8),
            StructureLevel(price=12.5, type="HIGH_60D", as_of=NOW, confidence=0.5),
        )
        result = RiskRewardService().evaluate(PROXY_VIEW, levels)
        assert result.invalidation == pytest.approx(9.5)
        assert result.target1 == pytest.approx(11.0)  # swing 压力优先于更近的区间高点
        assert result.downside == pytest.approx(0.05)
        assert result.upside == pytest.approx(0.10)
        assert result.rr == pytest.approx(2.0)
        # 锚点插值 (1.8,80)→(2.5,100)：t=0.2/0.7
        assert result.score == pytest.approx(85.7143, abs=1e-3)
        assert result.confidence == pytest.approx(0.8)  # min(两侧来源置信)

    def test_ma20_below_close_is_support_priority(self):
        """MA20 在 close 下方归支撑侧，优先于机械 60 日低点。"""
        levels = (
            StructureLevel(price=9.0909, type="LOW_60D", as_of=NOW, confidence=0.5),
            StructureLevel(price=9.8, type="MA20", as_of=NOW, confidence=0.6),
            StructureLevel(price=12.5, type="HIGH_60D", as_of=NOW, confidence=0.5),
        )
        result = RiskRewardService().evaluate(PROXY_VIEW, levels)
        assert result.invalidation == pytest.approx(9.8)  # 非机械 60 日最低点
        assert {level.type for level in result.levels} == {"MA20", "HIGH_60D"}

    def test_used_levels_recorded_with_evidence(self):
        levels = (
            StructureLevel(price=9.5, type="SWING_LOW_60M", as_of=NOW, confidence=0.8),
            StructureLevel(price=11.0, type="SWING_HIGH_60M", as_of=NOW, confidence=0.8),
        )
        result = RiskRewardService().evaluate(PROXY_VIEW, levels)
        used = {level.type: level for level in result.levels}
        assert used["SWING_LOW_60M"].price == pytest.approx(9.5)
        assert used["SWING_LOW_60M"].as_of == NOW
        assert used["SWING_LOW_60M"].confidence == pytest.approx(0.8)

    def test_insufficient_candidates_falls_back_to_proxy(self):
        """只有上方候选（无支撑）→ 回退 v1 代理路径，不空转不混合。"""
        levels = (
            StructureLevel(price=11.0, type="SWING_HIGH_60M", as_of=NOW, confidence=0.8),
            StructureLevel(price=12.5, type="HIGH_60D", as_of=NOW, confidence=0.5),
        )
        result = RiskRewardService().evaluate(PROXY_VIEW, levels)
        assert result.support == pytest.approx(10.0 / 1.10, abs=1e-4)  # 60d 代理
        assert result.upside == pytest.approx(0.25, abs=1e-6)
        assert result.levels == ()  # 回退路径不伪装结构明细

    def test_empty_levels_tuple_falls_back(self):
        result = RiskRewardService().evaluate(PROXY_VIEW, levels=())
        assert result.support == pytest.approx(10.0 / 1.10, abs=1e-4)
        assert result.levels == ()

    def test_price_equal_close_excluded(self):
        levels = (
            StructureLevel(price=10.0, type="MA20", as_of=NOW, confidence=0.6),
            StructureLevel(price=12.5, type="HIGH_60D", as_of=NOW, confidence=0.5),
        )
        result = RiskRewardService().evaluate(PROXY_VIEW, levels)
        # 10.0 与 close 相等不是有效支撑 → 回退 v1
        assert result.support == pytest.approx(10.0 / 1.10, abs=1e-4)


class TestMinute60Levels:
    def test_fact_builds_swing_levels(self):
        fact = {
            "state": "UP", "support": 9.5, "resistance": 11.2,
            "stale": False, "known_at": NOW,
        }
        levels = structure_levels_from_minute60(fact)
        by_type = {level.type: level for level in levels}
        assert by_type["SWING_LOW_60M"].price == pytest.approx(9.5)
        assert by_type["SWING_HIGH_60M"].price == pytest.approx(11.2)
        assert by_type["SWING_LOW_60M"].confidence == pytest.approx(0.8)
        assert by_type["SWING_LOW_60M"].as_of == NOW

    def test_stale_fact_not_a_fact(self):
        fact = {"state": "UP", "support": 9.5, "resistance": 11.2, "stale": True}
        assert structure_levels_from_minute60(fact) == ()

    def test_none_and_missing_prices(self):
        assert structure_levels_from_minute60(None) == ()
        assert structure_levels_from_minute60({"state": "UP"}) == ()
        assert len(structure_levels_from_minute60(
            {"state": "UP", "resistance": 11.2},
        )) == 1


class TestDefaultPathUnchanged:
    """levels 缺省 → v1 60 日代理路径完全不变（P0-06 语义）。"""

    def test_default_call_no_levels(self):
        result = RiskRewardService().evaluate(PROXY_VIEW)
        assert result.support == pytest.approx(9.0909, abs=1e-3)
        assert result.resistance == pytest.approx(12.5)
        assert result.upside == pytest.approx(0.25, abs=1e-4)
        assert result.rr == pytest.approx(2.75, abs=1e-2)
        assert result.score == pytest.approx(99.1667, abs=1e-3)
        assert result.confidence == 1.0
        assert result.levels == ()

    def test_existing_full_view_values_hold(self):
        """与 test_candidate_rank.py::TestRiskRewardScore 基线一致。"""
        result = RiskRewardService().evaluate(PROXY_VIEW)
        assert result.rr == pytest.approx(2.75, abs=1e-2)
        assert result.score == pytest.approx(99.1667, abs=1e-3)
