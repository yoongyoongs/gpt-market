from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.models import Kline, TechnicalIndicators


def _finite(value: float | np.floating | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), 4)


def calculate_indicators(klines: list[Kline], current_price: float | None = None) -> TechnicalIndicators:
    if not klines:
        return TechnicalIndicators()
    frame = pd.DataFrame([item.model_dump() for item in klines]).sort_values("timestamp")
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0).mask((loss == 0) & (gain == 0), 50.0)

    def ma(days: int) -> float | None:
        return _finite(close.rolling(days, min_periods=days).mean().iloc[-1])

    def window_value(series: pd.Series, days: int, operation: str) -> float | None:
        if len(series) < days:
            return None
        selected = series.tail(days)
        return _finite(selected.max() if operation == "max" else selected.min())

    def period_return(days: int) -> float | None:
        if len(close) <= days or close.iloc[-days - 1] == 0:
            return None
        return _finite((close.iloc[-1] / close.iloc[-days - 1] - 1) * 100)

    price = current_price if current_price is not None else float(close.iloc[-1])
    ma20 = ma(20)
    high20 = window_value(high, 20, "max")
    return TechnicalIndicators(
        ma5=ma(5), ma10=ma(10), ma20=ma20, ma60=ma(60),
        atr14=_finite(true_range.rolling(14, min_periods=14).mean().iloc[-1]),
        rsi14=_finite(rsi.iloc[-1]),
        high_20d=high20, low_20d=window_value(low, 20, "min"),
        high_60d=window_value(high, 60, "max"), low_60d=window_value(low, 60, "min"),
        distance_ma20_pct=None if not ma20 else _finite((price / ma20 - 1) * 100),
        distance_high_20d_pct=None if not high20 else _finite((price / high20 - 1) * 100),
        return_5d=period_return(5), return_20d=period_return(20),
    )
