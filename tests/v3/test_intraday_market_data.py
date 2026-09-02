"""RT-01：Intraday Market Data Adapter（实时方案 §4/§27）。

L0 Quote 快照 + 分钟/日/周 Bar 适配，全部复用 Legacy Provider：
- Quote/Bar 事实必须带 event_time/fetch_time/known_at/source/stale/quality；
- 未收盘 K 线显式 PROVISIONAL，绝不冒充正式历史；
- 单周期 Provider 故障隔离，其余周期不受影响。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Kline, KlineResult, Quote
from app.v3.application.intraday_market_data import IntradayMarketDataService
from app.v3.domain.intraday import IntradayQuoteSnapshot

NOW = datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc)
SH = timezone(timedelta(hours=8))


def _quote(**overrides) -> Quote:
    values = dict(
        code="000001", name="平安银行", market="SZ",
        price=9.32, prev_close=9.20, open=9.21, high=9.40, low=9.18,
        pct_change=1.30, change=0.12, volume=1_234_567, amount=11_500_000.0,
        turnover_rate=2.5, volume_ratio=1.8, amplitude=2.39,
        source="eastmoney", source_timestamp=NOW - timedelta(seconds=3),
        data_timestamp=NOW - timedelta(seconds=3),
        server_timestamp=NOW - timedelta(seconds=1),
        age_seconds=1.0, stale=False, quality="LIVE",
        timestamp_source="eastmoney", snapshot_id="snap-1",
        confidence="HIGH", suspended=False,
    )
    values.update(overrides)
    return Quote(**values)


def _bar(minute_offset: int, *, provisional: bool = False) -> Kline:
    return Kline(
        timestamp=NOW - timedelta(minutes=minute_offset),
        open=9.20, high=9.35, low=9.15, close=9.30,
        volume=10_000, amount=93_000.0, provisional=provisional,
    )


class _FakeKlineProvider:
    def __init__(self, klines_by_period: dict[str, list[Kline]]):
        self._klines = klines_by_period

    async def get_kline(self, code, period, limit, adjust="qfq"):
        if period not in self._klines:
            raise RuntimeError(f"no fixture for {period}")
        return KlineResult(
            code=code, period=period, klines=self._klines[period],
            source="tencent", source_timestamp=NOW,
            data_timestamp=NOW, server_timestamp=NOW, age_seconds=0.0,
            stale=False, quality="LIVE", timestamp_source="fetch_time",
            snapshot_id="snap-k", confidence="HIGH",
        )


@pytest.mark.asyncio
async def test_quote_snapshot_maps_legacy_quote_with_full_freshness() -> None:
    class _Provider:
        async def get_quote(self, code: str) -> Quote:
            return _quote()

    service = IntradayMarketDataService(_Provider())
    snapshot = await service.get_quote_snapshot("000001", as_of=NOW)

    assert isinstance(snapshot, IntradayQuoteSnapshot)
    assert snapshot.code == "000001"
    assert snapshot.market == "SZ"
    assert snapshot.last_price == 9.32
    assert snapshot.prev_close == 9.20
    assert snapshot.volume_ratio == 1.8
    assert snapshot.turnover_rate == 2.5
    # 时点三元组：行情时点 / 抓取时点 / 系统已知时点
    assert snapshot.event_time == NOW - timedelta(seconds=3)
    assert snapshot.fetch_time == snapshot.known_at == NOW - timedelta(seconds=1)
    assert snapshot.source == "eastmoney"
    assert snapshot.upstream_source == "eastmoney"
    assert snapshot.stale is False
    assert snapshot.quality == "LIVE"
    assert snapshot.suspended is False


@pytest.mark.asyncio
async def test_quote_snapshot_rejects_naive_as_of() -> None:
    class _Provider:
        async def get_quote(self, code: str) -> Quote:
            return _quote()

    service = IntradayMarketDataService(_Provider())
    with pytest.raises(ValueError):
        await service.get_quote_snapshot("000001", as_of=NOW.replace(tzinfo=None))


@pytest.mark.asyncio
async def test_intraday_bars_surface_status_provisional_and_stale() -> None:
    provider = _FakeKlineProvider({
        "5m": [_bar(10), _bar(5), _bar(1, provisional=True)],
        "day": [_bar(1440 * 5), _bar(60, provisional=True)],
    })
    service = IntradayMarketDataService(provider)
    result = await service.get_intraday_bars(
        "000001", periods=("5m", "day"), as_of=NOW,
    )
    assert result.known_at is not None
    five = result.periods["5m"]
    assert five.status == "AVAILABLE"
    assert five.bar_count == 3
    assert five.precision == "LIMITED"
    assert five.stale is False
    assert [bar.bar_status for bar in five.bars] == ["CLOSED", "CLOSED", "PROVISIONAL"]
    assert five.last_bar_time == _bar(1).timestamp
    day = result.periods["day"]
    assert day.status == "AVAILABLE"
    assert [bar.bar_status for bar in day.bars] == ["CLOSED", "PROVISIONAL"]
    # 未收盘 K 线显式 PROVISIONAL，绝不冒充正式历史
    assert any(bar.bar_status == "PROVISIONAL" for bar in day.bars)


@pytest.mark.asyncio
async def test_intraday_bars_isolate_provider_failure_per_period() -> None:
    provider = _FakeKlineProvider({"5m": [_bar(1)]})
    service = IntradayMarketDataService(provider)
    result = await service.get_intraday_bars(
        "000001", periods=("5m", "60m"), as_of=NOW,
    )
    assert result.periods["5m"].status == "AVAILABLE"
    failed = result.periods["60m"]
    assert failed.status == "UNKNOWN"
    assert failed.reason is not None and "RuntimeError" in failed.reason
    assert failed.bar_count == 0
    assert failed.precision == "UNKNOWN"


@pytest.mark.asyncio
async def test_intraday_bars_unknown_when_no_bars_known_at_as_of() -> None:
    provider = _FakeKlineProvider({"5m": [_bar(0), _bar(-5)]})
    service = IntradayMarketDataService(provider)
    result = await service.get_intraday_bars(
        "000001", periods=("5m",), as_of=NOW - timedelta(hours=2),
    )
    five = result.periods["5m"]
    assert five.status == "UNKNOWN"
    assert five.reason == "NO_BARS_KNOWN_AT_AS_OF"
