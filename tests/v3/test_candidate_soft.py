"""Step 4：Soft scoring 通用函数测试（任务书 §5/§35.1/§35.2）。

核心：边界 epsilon 连续性——不允许断崖。
"""

from __future__ import annotations

import pytest

from app.v3.candidate_engine.soft import (
    is_finite,
    position_52w_score,
    soft_band,
    soft_high_better,
    soft_low_better,
    soft_target,
    smoothstep,
    volume_5_20_score,
)


class TestSmoothstep:
    def test_endpoints(self):
        assert smoothstep(0.0) == 0.0
        assert smoothstep(1.0) == 1.0

    def test_clamped_outside(self):
        assert smoothstep(-5.0) == 0.0
        assert smoothstep(7.0) == 1.0

    def test_monotonic(self):
        values = [smoothstep(t / 100) for t in range(101)]
        assert values == sorted(values)


class TestSoftBand:
    def test_plateau_full_score(self):
        assert soft_band(0.5, 0.2, 0.3, 0.7, 0.8) == 100.0

    def test_outside_zero(self):
        assert soft_band(0.1, 0.2, 0.3, 0.7, 0.8) == 0.0
        assert soft_band(0.9, 0.2, 0.3, 0.7, 0.8) == 0.0

    def test_epsilon_continuity_left_edge(self):
        # 边界 epsilon：a±ε 输出连续，无断崖
        below = soft_band(0.2 - 1e-9, 0.2, 0.3, 0.7, 0.8)
        above = soft_band(0.2 + 1e-9, 0.2, 0.3, 0.7, 0.8)
        assert abs(below - above) < 1e-6

    def test_epsilon_continuity_right_edge(self):
        below = soft_band(0.8 - 1e-9, 0.2, 0.3, 0.7, 0.8)
        above = soft_band(0.8 + 1e-9, 0.2, 0.3, 0.7, 0.8)
        assert abs(below - above) < 1e-6

    def test_epsilon_continuity_plateau_edges(self):
        for edge in (0.3, 0.7):
            below = soft_band(edge - 1e-9, 0.2, 0.3, 0.7, 0.8)
            above = soft_band(edge + 1e-9, 0.2, 0.3, 0.7, 0.8)
            assert abs(below - above) < 1e-6

    def test_range_bounds(self):
        for x in [i / 20 for i in range(21)]:
            assert 0.0 <= soft_band(x, 0.2, 0.3, 0.7, 0.8) <= 100.0

    def test_invalid_shape_raises(self):
        with pytest.raises(ValueError):
            soft_band(0.5, 0.7, 0.3, 0.2, 0.8)


class TestLowHighBetter:
    def test_low_better_full_then_zero(self):
        assert soft_low_better(0.1, 0.3, 0.6) == 100.0
        assert soft_low_better(0.9, 0.3, 0.6) == 0.0

    def test_low_better_epsilon(self):
        left = soft_low_better(0.3 - 1e-9, 0.3, 0.6)
        right = soft_low_better(0.3 + 1e-9, 0.3, 0.6)
        assert abs(left - right) < 1e-6

    def test_high_better_with_floor(self):
        assert soft_high_better(0.1, 0.3, 0.6, floor=20.0) == 20.0
        assert soft_high_better(0.9, 0.3, 0.6, floor=20.0) == 100.0

    def test_high_better_epsilon(self):
        left = soft_high_better(0.6 - 1e-9, 0.3, 0.6)
        right = soft_high_better(0.6 + 1e-9, 0.3, 0.6)
        assert abs(left - right) < 1e-6

    def test_soft_target_is_band_alias(self):
        assert soft_target(0.5, 0.2, 0.3, 0.7, 0.8) == soft_band(0.5, 0.2, 0.3, 0.7, 0.8)


class TestPosition52w:
    """设计 §5.2.4：0~5% 只给 70 分，10~30% 100 分，>55% 递减。"""

    def test_zero_position_capped_at_70(self):
        assert position_52w_score(0.0) == 70.0

    def test_sweet_spot_full(self):
        assert position_52w_score(0.2) == 100.0

    def test_continuity_at_5pct(self):
        low = position_52w_score(0.05 - 1e-9)
        high = position_52w_score(0.05 + 1e-9)
        assert abs(low - high) < 1e-6
        assert abs(low - 70.0) < 1e-6

    def test_continuity_at_30pct(self):
        low = position_52w_score(0.30 - 1e-9)
        high = position_52w_score(0.30 + 1e-9)
        assert abs(low - high) < 1e-6

    def test_high_position_decays(self):
        assert position_52w_score(0.9) < 30.0
        assert position_52w_score(1.0) == 0.0

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            position_52w_score(1.2)


class TestVolume520:
    """设计 §8.4：高分区 1.05~1.60，>2.5 不加分。"""

    def test_moderate_expansion_full(self):
        assert volume_5_20_score(1.3) == 100.0

    def test_explosive_volume_zero(self):
        assert volume_5_20_score(2.6) == 0.0

    def test_epsilon_at_2_5(self):
        left = volume_5_20_score(2.5 - 1e-9)
        right = volume_5_20_score(2.5 + 1e-9)
        assert abs(left - right) < 1e-6


class TestIsFinite:
    def test_none_is_missing(self):
        assert is_finite(None) is False

    def test_nan_is_invalid(self):
        assert is_finite(float("nan")) is False

    def test_inf_is_invalid(self):
        assert is_finite(float("inf")) is False

    def test_zero_is_valid(self):
        assert is_finite(0.0) is True
