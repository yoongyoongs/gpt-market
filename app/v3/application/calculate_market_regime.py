from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from app.v3.application.calculate_features import mean_available
from app.v3.domain.features import MarketRegimeSnapshot, SecurityFeature


class CalculateMarketRegimeService:
    def execute(
        self,
        *,
        feature_run_id: UUID,
        features: tuple[SecurityFeature, ...],
        as_of: datetime,
        known_at: datetime,
        expected_count: int,
    ) -> MarketRegimeSnapshot:
        usable = tuple(item for item in features if not item.stale)
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
            index_states={"status": "UNKNOWN", "reason": "index benchmark not bound to this feature run"},
            breadth={
                "observed": len(returns),
                "advancing": advancing,
                "declining": declining,
                "unchanged": unchanged,
                "advance_decline_ratio": advancing / declining if declining else None,
                "mean_return_3d": mean_available(returns),
            },
            turnover={"observed": sum(item.amount is not None for item in usable), "total_amount": total_amount},
            limit_structure={"status": "UNKNOWN", "reason": "board-specific price-limit facts unavailable"},
            size_style={"status": "UNKNOWN", "reason": "market-cap facts unavailable"},
            growth_value_style={"status": "UNKNOWN", "reason": "style benchmark facts unavailable"},
            industry_rotation={"status": "UNKNOWN", "reason": "industry classification unavailable"},
            risk_appetite_facts={
                "volume_expansion_count": expansion_count,
                "breakout_20d_count": breakout_count,
                "stale_count": len(features) - len(usable),
            },
            coverage=coverage,
            confidence=min(coverage, len(returns) / expected_count if expected_count else 0.0),
            stale=not usable or any(item.stale for item in features),
        )
