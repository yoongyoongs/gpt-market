from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from app.v3.application.calculate_features import mean_available
from app.v3.application.calculate_index_benchmark_return import (
    IndexBenchmarkReturnResult,
)
from app.v3.domain.features import MarketRegimeSnapshot, SecurityFeature


# RC-04-04 冻结语义：指数 20 日收益 >= +2% 为 UP、<= -2% 为 DOWN、
# 其余为 RANGE；基准缺失或历史不足时保持 UNKNOWN（原因透传）。
INDEX_STATE_UP_THRESHOLD = 0.02
INDEX_STATE_DOWN_THRESHOLD = -0.02

# RT §23.3（用户拍板）：regime stale 改比例阈值——不再因单票 stale 拖垮全局。
# stale 行占比超阈值，或全部行 stale，才判 stale=true；原因如实透传 stale_reason。
REGIME_STALE_RATIO_THRESHOLD = 0.2
STALE_RULE_VERSION = "regime-stale-ratio-v1"


class CalculateMarketRegimeService:
    def __init__(self, *, stale_ratio_threshold: float = REGIME_STALE_RATIO_THRESHOLD) -> None:
        if not 0 < stale_ratio_threshold <= 1:
            raise ValueError("stale_ratio_threshold must be in (0, 1]")
        self._stale_ratio_threshold = stale_ratio_threshold

    def execute(
        self,
        *,
        feature_run_id: UUID,
        features: tuple[SecurityFeature, ...],
        as_of: datetime,
        known_at: datetime,
        expected_count: int,
        index_benchmark: IndexBenchmarkReturnResult | None = None,
    ) -> MarketRegimeSnapshot:
        usable = tuple(item for item in features if not item.stale)
        stale_count = len(features) - len(usable)
        stale_ratio = stale_count / len(features) if features else 0.0
        if not features:
            stale = True
            stale_cause = "no_rows"
        elif not usable:
            stale = True
            stale_cause = "all_rows_stale"
        elif stale_ratio > self._stale_ratio_threshold:
            stale = True
            stale_cause = "stale_ratio_above_threshold"
        else:
            stale = False
            stale_cause = None
        stale_reason = {
            "rule_version": STALE_RULE_VERSION,
            "threshold": self._stale_ratio_threshold,
            "stale_count": stale_count,
            "total_count": len(features),
            "stale_ratio": stale_ratio,
            "cause": stale_cause,
        }
        returns = [item.return_3d for item in usable if item.return_3d is not None]
        advancing = sum(value > 0 for value in returns)
        declining = sum(value < 0 for value in returns)
        unchanged = sum(value == 0 for value in returns)
        total_amount = sum(item.amount for item in usable if item.amount is not None)
        expansion_count = sum(item.volume_expansion is True for item in usable)
        breakout_count = sum(item.breakout_20d is True for item in usable)
        coverage = len(features) / expected_count if expected_count else 0.0
        return MarketRegimeSnapshot.build(
            regime_snapshot_id=uuid4(),
            feature_run_id=feature_run_id,
            as_of=as_of,
            known_at=known_at,
            index_states=self._index_states(index_benchmark),
            breadth={
                "observed": len(returns),
                "advancing": advancing,
                "declining": declining,
                "unchanged": unchanged,
                "advance_decline_ratio": advancing / declining if declining else None,
                "mean_return_3d": mean_available(returns),
            },
            turnover={
                "observed": sum(item.amount is not None for item in usable),
                "total_amount": total_amount,
                "coverage": (
                    sum(item.amount is not None for item in usable) / expected_count
                    if expected_count else 0.0
                ),
            },
            limit_structure={"status": "UNKNOWN", "reason": "board-specific price-limit facts unavailable"},
            size_style={"status": "UNKNOWN", "reason": "market-cap facts unavailable"},
            growth_value_style={"status": "UNKNOWN", "reason": "style benchmark facts unavailable"},
            industry_rotation={"status": "UNKNOWN", "reason": "industry classification unavailable"},
            risk_appetite_facts={
                "volume_expansion_count": expansion_count,
                "breakout_20d_count": breakout_count,
                "stale_count": stale_count,
            },
            coverage=coverage,
            confidence=min(coverage, len(returns) / expected_count if expected_count else 0.0),
            stale=stale,
            stale_reason=stale_reason,
        )

    @staticmethod
    def _index_states(index_benchmark: IndexBenchmarkReturnResult | None) -> dict:
        if index_benchmark is None:
            return {
                "status": "UNKNOWN",
                "reason": "index benchmark not bound to this feature run",
            }
        if index_benchmark.return_20d is None:
            return {
                "status": "UNKNOWN",
                "reason": index_benchmark.reason or "NO_REVISION",
                "benchmark_code": index_benchmark.benchmark_code,
                "calculation_version": index_benchmark.calculation_version,
            }
        if index_benchmark.return_20d >= INDEX_STATE_UP_THRESHOLD:
            status = "UP"
        elif index_benchmark.return_20d <= INDEX_STATE_DOWN_THRESHOLD:
            status = "DOWN"
        else:
            status = "RANGE"
        return {
            "status": status,
            "benchmark_code": index_benchmark.benchmark_code,
            "return_20d": index_benchmark.return_20d,
            "known_at": (
                index_benchmark.known_at.isoformat()
                if index_benchmark.known_at is not None else None
            ),
            "source": index_benchmark.source,
            "calculation_version": index_benchmark.calculation_version,
        }
