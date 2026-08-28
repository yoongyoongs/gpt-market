from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import Settings
from app.kline_cache import KlineCache
from app.models import Kline, KlineResult
from app.providers.base import ProviderEmptyDataError, ProviderUnsupportedError
from app.providers.manager import ProviderManager
from app.services.data_quality import DataQualityService
from app.services.kline_aggregation import aggregate_5m_klines, aggregate_day_klines, mark_latest_bar_partial
from app.services.market_data_service import MarketDataService
from app.utils.time import SHANGHAI, now_shanghai
from tests.test_multi_provider_kline_cache import quote


QUALITY = DataQualityService()


def bar(timestamp: datetime, price: float = 10.0) -> Kline:
    return Kline(
        timestamp=timestamp,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price + 0.5,
        volume=100,
        amount=1000,
    )


def day_rows(start: datetime, count: int) -> list[Kline]:
    return [bar(start + timedelta(days=offset), 10 + offset) for offset in range(count)]


def test_aggregate_day_to_week_and_marks_current_week_partial() -> None:
    start = datetime(2026, 8, 17, tzinfo=SHANGHAI)
    rows = day_rows(start, 8)
    now = datetime(2026, 8, 26, 10, 0, tzinfo=SHANGHAI)
    weeks = aggregate_day_klines(rows, "week", now, 10)
    assert len(weeks) == 2
    assert weeks[0].open == rows[0].open
    assert weeks[0].close == rows[6].close
    assert weeks[0].provisional is False
    assert weeks[1].timestamp == rows[-1].timestamp
    assert weeks[1].provisional is True


def test_aggregate_day_marks_current_week_complete_on_weekend() -> None:
    start = datetime(2026, 8, 24, tzinfo=SHANGHAI)
    rows = day_rows(start, 5)
    now = datetime(2026, 8, 29, 9, 0, tzinfo=SHANGHAI)
    weeks = aggregate_day_klines(rows, "week", now, 10)
    assert len(weeks) == 1
    assert weeks[0].provisional is False


def test_aggregate_5m_to_60m_does_not_cross_lunch_and_marks_partial() -> None:
    now = datetime(2026, 8, 28, 13, 20, tzinfo=SHANGHAI)
    rows = [
        bar(datetime(2026, 8, 28, hour, minute, tzinfo=SHANGHAI), 10 + index)
        for index, (hour, minute) in enumerate(
            [(9, 35), (9, 40), (9, 45), (9, 50), (9, 55), (10, 0), (10, 5), (10, 10), (10, 15), (10, 20), (10, 25), (10, 30)],
            1,
        )
    ]
    rows.extend(
        [
            bar(datetime(2026, 8, 28, 10, minute, tzinfo=SHANGHAI), 30 + index)
            for index, minute in enumerate((35, 40, 45, 50, 55), 1)
        ]
    )
    rows.append(bar(datetime(2026, 8, 28, 11, 0, tzinfo=SHANGHAI), 39))
    rows.extend(
        [
            bar(datetime(2026, 8, 28, 13, minute, tzinfo=SHANGHAI), 40 + index)
            for index, minute in enumerate((5, 10, 15), 1)
        ]
    )
    hourly = aggregate_5m_klines(rows, "60m", now, 10)
    assert [item.timestamp.time().isoformat(timespec="minutes") for item in hourly] == ["10:30", "11:00", "13:15"]
    assert hourly[0].provisional is False
    assert hourly[1].provisional is True
    assert hourly[2].provisional is True


def test_direct_current_60m_bar_is_marked_partial() -> None:
    current = bar(datetime(2026, 8, 28, 10, 5, tzinfo=SHANGHAI))
    marked = mark_latest_bar_partial(
        [current], "60m", datetime(2026, 8, 28, 10, 20, tzinfo=SHANGHAI)
    )
    assert marked[-1].provisional is True


def test_direct_closed_60m_bar_is_not_marked_partial() -> None:
    closed = bar(datetime(2026, 8, 28, 10, 30, tzinfo=SHANGHAI))
    marked = mark_latest_bar_partial(
        [closed], "60m", datetime(2026, 8, 28, 10, 40, tzinfo=SHANGHAI)
    )
    assert marked[-1].provisional is False


class PeriodFailProvider:
    name = "eastmoney"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def start(self): pass
    async def close(self): pass
    async def get_quote(self, code): return quote(code)
    async def get_quotes(self, codes): return [quote(code) for code in codes]
    async def get_index_quote(self, code, market): return quote(code)

    async def get_kline(self, code, period, limit, adjust="qfq", *, quote=None):
        self.calls.append((period, limit))
        if period == "day":
            now = now_shanghai()
            rows = [bar(now - timedelta(days=offset), 10 + offset) for offset in range(90, 0, -1)]
            return KlineResult(code=code, period=period, klines=rows, **QUALITY.assess(now, source="eastmoney"))
        raise ProviderEmptyDataError(f"no {period}")

    async def get_all_a_shares(self):
        raise ProviderUnsupportedError("not needed")

    async def get_sector_ranking(self, sector_type, limit):
        raise ProviderUnsupportedError("not needed")


@pytest.mark.asyncio
async def test_market_data_service_aggregates_week_after_provider_failure(tmp_path) -> None:
    eastmoney = PeriodFailProvider()
    tencent = PeriodFailProvider()
    manager = ProviderManager(eastmoney, tencent, attempts_per_provider=1)
    settings = Settings(kline_cache_path=str(tmp_path / "klines.sqlite3"), max_kline_concurrency=2)
    service = MarketDataService(manager, KlineCache(settings.kline_cache_path), QUALITY, settings)
    await service.start()
    result = await service.get_kline("603019", "week", 20, "qfq")
    assert result.period == "week"
    assert result.source.startswith("aggregate:day:")
    assert len(result.klines) >= 10
    assert ("week", 20) in eastmoney.calls
    assert any(period == "day" for period, _ in eastmoney.calls)
    await service.close()
