from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.indicators.technical import calculate_indicators
from app.models import Kline
from app.utils.time import SHANGHAI


def make_klines(count: int = 60) -> list[Kline]:
    start = datetime(2026, 1, 1, tzinfo=SHANGHAI)
    return [
        Kline(timestamp=start + timedelta(days=index), open=value, high=value + 1, low=value - 1, close=value, volume=10000, amount=100000)
        for index, value in enumerate(range(1, count + 1))
    ]


def test_moving_averages() -> None:
    result = calculate_indicators(make_klines())
    assert result.ma5 == 58.0
    assert result.ma20 == 50.5
    assert result.ma60 == 30.5


def test_atr14_and_rsi14() -> None:
    result = calculate_indicators(make_klines())
    assert result.atr14 == 2.0
    assert result.rsi14 == 100.0


def test_period_ranges_and_returns() -> None:
    result = calculate_indicators(make_klines())
    assert result.high_20d == 61.0
    assert result.low_60d == 0.0
    assert result.return_5d == pytest.approx(9.0909, abs=0.0001)
