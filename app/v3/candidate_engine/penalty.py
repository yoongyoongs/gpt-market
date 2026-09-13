"""Penalty Engine（设计 §20）：追涨/过热/结构恶化扣分，封顶 -30。

v1 可计算规则（其余规则无数据源，实施报告列明）：
- 20 日涨幅过热（>25% -5 / >40% -12）
- 距 MA20 过远（>15% -8）
- 连续爆量加速（volume_spike 与连涨组合 -5~-12）
- 周K 严重下降（8 周斜率 < -3% -5）
- 业绩明显恶化（同报告期同比 < -10% -10，FQ 证据复用）
- 近端压力过近（贴 60 日高 -5~-10）
"""

from __future__ import annotations

from app.v3.candidate_engine.soft import is_finite
from app.v3.candidate_engine.experts.evidence_experts import (
    PROFIT_FIELD,
    REVENUE_FIELD,
    _same_period_growth,
)
from app.v3.domain.candidate_engine import (
    MAX_PENALTY,
    ExpertFeatureView,
    ExpertInput,
    PenaltyAssessment,
)

__all__ = ["PenaltyEngine"]

_WORSEN_GROWTH = -0.10


class PenaltyEngine:
    """逐规则累计扣分，总封顶 MAX_PENALTY（§20，Safety 硬风险除外）。"""

    def evaluate(self, stock: ExpertInput) -> PenaltyAssessment:
        items: list[tuple[str, float, str]] = []
        self._overheat(stock.feature, items)
        self._stretched_from_ma(stock.feature, items)
        self._volume_acceleration(stock.feature, items)
        self._weekly_severe_decline(stock.feature, items)
        self._fundamental_worsening(stock, items)
        self._nearby_resistance(stock.feature, items)
        total = max(-MAX_PENALTY, -min(sum(points for _, points, _ in items), MAX_PENALTY))
        total = 0.0 if not items else total
        return PenaltyAssessment(
            total=round(total, 2),
            items=tuple((name, points, detail) for name, points, detail in items),
        )

    def _overheat(self, view: ExpertFeatureView, items: list) -> None:
        return_20d = view.number("return_20d")
        if not is_finite(return_20d):
            return
        assert return_20d is not None
        if return_20d > 0.40:
            items.append(("return_20d_overheat", 12.0, f"return_20d={return_20d:.3f}"))
        elif return_20d > 0.25:
            items.append(("return_20d_overheat", 5.0, f"return_20d={return_20d:.3f}"))

    def _stretched_from_ma(self, view: ExpertFeatureView, items: list) -> None:
        distance = view.number("price_ma20_distance")
        if is_finite(distance) and abs(distance) > 0.15:  # type: ignore[arg-type]
            items.append(("stretched_from_ma20", 8.0, f"distance={distance:.3f}"))

    def _volume_acceleration(self, view: ExpertFeatureView, items: list) -> None:
        spike = view.number("volume_spike")
        up_days = view.number("consecutive_up_days")
        if not is_finite(spike):
            return
        assert spike is not None
        if spike > 2.5 and is_finite(up_days) and up_days >= 5:  # type: ignore[arg-type]
            items.append(("volume_acceleration", 12.0, f"spike={spike:.2f},up={up_days:g}"))
        elif spike > 2.5:
            items.append(("volume_acceleration", 5.0, f"spike={spike:.2f}"))

    def _weekly_severe_decline(self, view: ExpertFeatureView, items: list) -> None:
        slope = view.number("weekly_slope_8w")
        if is_finite(slope) and slope < -0.03:  # type: ignore[arg-type]
            items.append(("weekly_severe_decline", 5.0, f"slope_8w={slope:.4f}"))

    def _fundamental_worsening(self, stock: ExpertInput, items: list) -> None:
        if not stock.evidence:
            return
        revenue_growth, profit_growth = _same_period_growth(list(stock.evidence))
        for field_name, growth in ((REVENUE_FIELD, revenue_growth), (PROFIT_FIELD, profit_growth)):
            if growth is not None and growth < _WORSEN_GROWTH:
                items.append((
                    "fundamental_worsening", 10.0,
                    f"{field_name} yoy={growth:.3f}",
                ))
                return

    def _nearby_resistance(self, view: ExpertFeatureView, items: list) -> None:
        dist_high = view.number("distance_60d_high")
        if not is_finite(dist_high):
            return
        assert dist_high is not None
        gap = abs(dist_high)
        if gap < 0.01:
            items.append(("nearby_resistance", 10.0, f"gap_to_60d_high={gap:.4f}"))
        elif gap < 0.03:
            items.append(("nearby_resistance", 5.0, f"gap_to_60d_high={gap:.4f}"))
