from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.v3.domain.recall import RecallChannel, RecallFeatureView
from app.v3.domain.evidence import SecurityEvidenceView
from app.v3.providers.recall import (
    ChannelEvaluation,
    RecallCandidate,
    RecallChannelUnavailable,
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _value(feature: RecallFeatureView, name: str) -> Any:
    if name.startswith("features."):
        return feature.features.get(name.partition(".")[2])
    return getattr(feature, name)


class FeatureRecallChannel:
    def __init__(
        self,
        *,
        code: str,
        description: str,
        required_fields: tuple[str, ...],
        predicate: Callable[[RecallFeatureView], bool],
        strength: Callable[[RecallFeatureView], float],
        version: str = "feature-recall-v1",
        configuration: dict[str, Any] | None = None,
    ) -> None:
        self.channel = RecallChannel.build(
            code=code,
            version=version,
            configuration={
                "required_fields": required_fields,
                **(configuration or {}),
            },
            description=description,
        )
        self._required_fields = required_fields
        self._predicate = predicate
        self._strength = strength

    def evaluate(
        self,
        features: tuple[RecallFeatureView, ...],
        evidence: tuple[SecurityEvidenceView, ...] = (),
    ) -> ChannelEvaluation:
        candidates = []
        evaluated = 0
        unavailable = 0
        for feature in features:
            values = {name: _value(feature, name) for name in self._required_fields}
            if feature.stale or any(value is None or value == "UNKNOWN" for value in values.values()):
                unavailable += 1
                continue
            evaluated += 1
            if not self._predicate(feature):
                continue
            candidates.append(RecallCandidate(
                security_id=feature.security_id,
                strength=_clamp(self._strength(feature)),
                reasons=tuple(f"{name}={values[name]}" for name in self._required_fields),
                matched_features=values,
                coverage=feature.coverage,
            ))
        if evaluated == 0:
            raise RecallChannelUnavailable(
                f"{self.channel.code} has no non-stale rows with required fields"
            )
        candidates.sort(key=lambda item: (-item.strength, str(item.security_id)))
        return ChannelEvaluation(
            evaluated_count=evaluated,
            unavailable_count=unavailable,
            candidates=tuple(candidates),
        )


def feature_recall_channels() -> tuple[FeatureRecallChannel, ...]:
    return (
        FeatureRecallChannel(
            code="LOW_POSITION_TURNING",
            description="低位区间内短期转强且中期斜率非负",
            required_fields=("position_60d", "return_3d", "ma20_slope"),
            predicate=lambda f: f.position_60d <= 0.4 and f.return_3d > 0 and f.ma20_slope >= 0,
            strength=lambda f: (
                _clamp((0.4 - f.position_60d) / 0.4)
                + _clamp(f.return_3d / 0.05)
                + _clamp(f.ma20_slope / 0.01)
            ) / 3,
            configuration={"max_position_60d": 0.4, "min_return_3d": 0, "min_ma20_slope": 0},
        ),
        FeatureRecallChannel(
            code="TREND_IGNITION",
            description="短期收益扩张且中期趋势刚转强",
            required_fields=("return_5d", "ma20_slope", "position_60d"),
            predicate=lambda f: f.return_5d >= 0.03 and f.ma20_slope > 0 and f.position_60d < 0.85,
            strength=lambda f: (
                _clamp(f.return_5d / 0.1)
                + _clamp(f.ma20_slope / 0.015)
                + _clamp((0.85 - f.position_60d) / 0.85)
            ) / 3,
            configuration={"min_return_5d": 0.03, "max_position_60d": 0.85},
        ),
        FeatureRecallChannel(
            code="FIRST_BREAKOUT",
            description="首次收盘突破近二十日区间",
            required_fields=("breakout_20d", "return_3d"),
            predicate=lambda f: f.breakout_20d is True and f.return_3d > 0,
            strength=lambda f: 0.5 + 0.5 * _clamp(f.return_3d / 0.08),
        ),
        FeatureRecallChannel(
            code="FIRST_PULLBACK",
            description="上行趋势中的首次均线回踩",
            required_fields=("pullback_20d", "return_20d", "ma20_slope"),
            predicate=lambda f: f.pullback_20d is True and f.return_20d > 0 and f.ma20_slope > 0,
            strength=lambda f: (
                _clamp(f.return_20d / 0.2) + _clamp(f.ma20_slope / 0.01)
            ) / 2,
        ),
        FeatureRecallChannel(
            code="WEEK_BASE_DAY_STRENGTH",
            description="周级底座明确且日级趋势转强",
            required_fields=("features.weekly_trend_state", "features.daily_trend_state", "return_5d"),
            predicate=lambda f: (
                f.features["weekly_trend_state"] in {"BASE", "UP"}
                and f.features["daily_trend_state"] == "UP"
                and f.return_5d > 0
            ),
            strength=lambda f: 0.6 + 0.4 * _clamp(f.return_5d / 0.08),
        ),
        FeatureRecallChannel(
            code="RELATIVE_INDEX_STRENGTH",
            description="相对主要指数取得显著超额收益",
            required_fields=("relative_index_strength",),
            predicate=lambda f: f.relative_index_strength >= 0.03,
            strength=lambda f: _clamp(f.relative_index_strength / 0.12),
            configuration={"minimum_excess_return": 0.03},
        ),
        FeatureRecallChannel(
            code="RELATIVE_INDUSTRY_STRENGTH",
            description="相对所属行业取得显著超额收益",
            required_fields=("relative_industry_strength",),
            predicate=lambda f: f.relative_industry_strength >= 0.03,
            strength=lambda f: _clamp(f.relative_industry_strength / 0.12),
            configuration={"minimum_excess_return": 0.03},
        ),
        FeatureRecallChannel(
            code="VOLUME_EXPANSION",
            description="成交量显著扩张且短期价格确认",
            required_fields=("volume_expansion", "volume_ratio_5d", "return_3d"),
            predicate=lambda f: f.volume_expansion is True and f.return_3d > 0,
            strength=lambda f: (
                _clamp((f.volume_ratio_5d - 1) / 2) + _clamp(f.return_3d / 0.06)
            ) / 2,
        ),
        FeatureRecallChannel(
            code="ANOMALY_COMBINATION",
            description="突破、量能和短期收益同时异常",
            required_fields=("breakout_20d", "volume_expansion", "return_3d"),
            predicate=lambda f: f.breakout_20d is True and f.volume_expansion is True and f.return_3d >= 0.03,
            strength=lambda f: 0.7 + 0.3 * _clamp(f.return_3d / 0.1),
        ),
    )
