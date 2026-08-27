from __future__ import annotations

from app.indicators import calculate_indicators
from app.models import Kline, TechnicalIndicators


class TechnicalIndicatorService:
    """Single calculation entry point used by details and market scans."""

    def calculate(self, klines: list[Kline], current_price: float | None = None) -> TechnicalIndicators:
        return calculate_indicators(klines, current_price)
