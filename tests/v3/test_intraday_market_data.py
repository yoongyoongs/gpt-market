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
async def test_intraday_bars_carry_per_period_provenance() -> None:
    """R4-P1-006 §18.1：每周期 source/upstream/known_at/quality/
    confidence/fallback_used——直连主源与 aggregate/缓存 fallback 可辨。"""
    class _MixedProvider:
        async def get_kline(self, code, period, limit, adjust="qfq"):
            if period == "60m":
                source, ts_source = "eastmoney", "eastmoney"
            elif period == "15m":
                source, ts_source = "tencent", "tencent"
            else:  # week：日 K 聚合
                source, ts_source = "aggregate:day:tencent", "fetch_time"
            return KlineResult(
                code=code, period=period, klines=[_bar(5)],
                source=source, source_timestamp=NOW,
                data_timestamp=NOW - timedelta(seconds=2),
                server_timestamp=NOW, age_seconds=2.0,
                stale=False, quality="LIVE", timestamp_source=ts_source,
                snapshot_id=f"snap-{period}", confidence="MEDIUM",
            )

    service = IntradayMarketDataService(_MixedProvider())
    result = await service.get_intraday_bars(
        "000001", periods=("60m", "15m", "week"), as_of=NOW,
    )
    hour = result.periods["60m"]
    assert hour.source == "eastmoney"
    assert hour.upstream_source == "eastmoney"
    assert hour.fallback_used is False
    assert hour.known_at == NOW - timedelta(seconds=2)
    assert hour.quality == "LIVE" and hour.confidence == "MEDIUM"
    fifteen = result.periods["15m"]
    assert fifteen.source == "tencent"
    assert fifteen.fallback_used is True
    week = result.periods["week"]
    assert week.source == "aggregate:day:tencent"
    assert week.upstream_source == "fetch_time"
    assert week.fallback_used is True


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


def _falling_bars(count: int, start: float = 40.0) -> list[Kline]:
    bars = []
    for i in range(count):
        close = start - i * 0.5
        bars.append(Kline(
            timestamp=NOW - timedelta(weeks=count - i),
            open=close + 0.2, high=close + 0.3, low=close - 0.3, close=close,
            volume=1_000, amount=40_000.0,
        ))
    return bars


def _rising_bars(count: int, start: float = 10.0) -> list[Kline]:
    bars = []
    for i in range(count):
        close = start + i * 0.1
        bars.append(Kline(
            timestamp=NOW - timedelta(days=count - i),
            open=close - 0.05, high=close + 0.05, low=close - 0.1, close=close,
            volume=1_000, amount=10_000.0,
        ))
    return bars


class _FakeBarsService:
    def __init__(self, periods: dict[str, tuple[str, list[Kline]]]):
        # period -> (status, klines)；status=UNKNOWN 时模拟该周期故障
        self._periods = periods

    async def get_intraday_bars(self, code, periods=("1m",), *, as_of):
        from app.v3.domain.intraday import IntradayBarSeries

        series = {}
        for period in periods:
            if period not in self._periods:
                series[period] = IntradayBarSeries(
                    period=period, status="UNKNOWN", reason="NO_FIXTURE",
                    precision="UNKNOWN",
                )
                continue
            status, klines = self._periods[period]
            if status == "UNKNOWN":
                series[period] = IntradayBarSeries(
                    period=period, status="UNKNOWN",
                    reason="RuntimeError: up down", precision="UNKNOWN",
                )
                continue
            provisional = any(bar.provisional for bar in klines)
            series[period] = IntradayBarSeries(
                period=period, status="AVAILABLE",
                bars=tuple(
                    __import__("app.v3.domain.intraday", fromlist=["IntradayBar"]).IntradayBar(
                        bar_time=bar.timestamp, open=bar.open, high=bar.high,
                        low=bar.low, close=bar.close, volume=bar.volume,
                        amount=bar.amount,
                        bar_status="PROVISIONAL" if bar.provisional else "CLOSED",
                    ) for bar in klines
                ),
                bar_count=len(klines), provisional=provisional,
                stale=False, precision="LIMITED",
                first_bar_time=klines[0].timestamp,
                last_bar_time=klines[-1].timestamp,
            )
        from app.v3.domain.intraday import IntradayBarsResult as _R
        return _R(code=code, as_of=as_of, known_at=as_of,
                  source="fixture", periods=series)


@pytest.mark.asyncio
async def test_structure_snapshot_weekly_down_daily_up_is_bounce() -> None:
    from app.v3.application.intraday_structure_snapshot import (
        IntradayStructureSnapshotService,
    )

    bars = _FakeBarsService({
        "week": ("AVAILABLE", _falling_bars(12)),
        "day": ("AVAILABLE", _rising_bars(12)),
        "60m": ("AVAILABLE", _rising_bars(12)),
        "15m": ("AVAILABLE", _rising_bars(12)),
        "5m": ("AVAILABLE", _rising_bars(12)),
    })
    service = IntradayStructureSnapshotService(bars)
    snapshot = await service.get_snapshot("000001", as_of=NOW)

    assert snapshot.weekly.trend == "DOWN"
    assert snapshot.daily.trend == "UP"
    # §4.5：周降日涨必须显式表达为"下降趋势中的反弹候选"，绝不静默
    assert snapshot.reversal_state == "POSSIBLE"
    assert snapshot.conflict == "WEEKLY_DOWN_DAILY_BOUNCE"
    assert snapshot.conflict_rule == "下降趋势中的反弹"
    # 60m 结构可用：趋势 + 支撑 + 压力
    assert snapshot.periods["60m"].trend == "UP"
    assert snapshot.periods["60m"].support is not None
    assert snapshot.periods["60m"].resistance is not None
    assert snapshot.stale is False


@pytest.mark.asyncio
async def test_structure_snapshot_marks_provisional_and_unknown() -> None:
    from app.v3.application.intraday_structure_snapshot import (
        IntradayStructureSnapshotService,
    )

    daily = _rising_bars(11) + [
        Kline(timestamp=NOW, open=11.0, high=11.1, low=10.9, close=11.05,
              volume=1, amount=1.0, provisional=True)
    ]
    bars = _FakeBarsService({
        "week": ("AVAILABLE", _falling_bars(3)),  # 不足 8 根 → UNKNOWN
        "day": ("AVAILABLE", daily),
        "60m": ("UNKNOWN", []),
        "15m": ("AVAILABLE", _rising_bars(12)),
        "5m": ("AVAILABLE", _rising_bars(12)),
    })
    service = IntradayStructureSnapshotService(bars)
    snapshot = await service.get_snapshot("000001", as_of=NOW)

    # 周线数据不足：UNKNOWN + INSUFFICIENT_BARS，绝不给伪支撑压力
    assert snapshot.weekly.trend == "UNKNOWN"
    assert snapshot.weekly.support is None and snapshot.weekly.resistance is None
    assert snapshot.weekly.reason == "INSUFFICIENT_BARS"
    # 反转状态随关键周期缺失 → UNKNOWN
    assert snapshot.reversal_state == "UNKNOWN"
    assert snapshot.conflict == "UNKNOWN"
    # 盘中日 K provisional 显式透传
    assert snapshot.daily.bar_status == "PROVISIONAL"
    # 60m 故障隔离：不拖垮其它周期
    assert snapshot.periods["60m"].trend == "UNKNOWN"
    assert snapshot.periods["15m"].trend == "UP"


@pytest.mark.asyncio
async def test_structure_snapshot_latest_price_from_quote_is_optional() -> None:
    from app.v3.application.intraday_structure_snapshot import (
        IntradayStructureSnapshotService,
    )

    bars = _FakeBarsService({
        "week": ("AVAILABLE", _falling_bars(12)),
        "day": ("AVAILABLE", _rising_bars(12)),
        "60m": ("AVAILABLE", _rising_bars(12)),
        "15m": ("AVAILABLE", _rising_bars(12)),
        "5m": ("AVAILABLE", _rising_bars(12)),
    })

    class _QuoteService:
        async def get_quote_snapshot(self, code, *, as_of):
            return await IntradayMarketDataService(_QuoteProvider()).get_quote_snapshot(
                code, as_of=as_of
            )

    class _QuoteProvider:
        async def get_quote(self, code):
            return _quote()

    with_price = IntradayStructureSnapshotService(bars, quote_service=_QuoteService())
    snapshot = await with_price.get_snapshot("000001", as_of=NOW)
    assert snapshot.latest_price == 9.32

    without_price = IntradayStructureSnapshotService(bars)
    snapshot2 = await without_price.get_snapshot("000001", as_of=NOW)
    assert snapshot2.latest_price is None


@pytest.mark.asyncio
async def test_service_wired_to_provider_manager_falls_back_to_tencent() -> None:
    """R3-P1-006：V3 实时主入口接 ProviderManager——主 Provider 故障时
    自动落到次级（腾讯），不再绑死单 EastmoneyProvider。"""
    from app.providers.manager import ProviderManager

    class _FailingEast:
        async def get_quote(self, code: str) -> Quote:
            raise RuntimeError("eastmoney down")

    class _FakeTencent:
        def __init__(self):
            self.calls = 0

        async def get_quote(self, code: str) -> Quote:
            self.calls += 1
            return _quote(source="tencent", timestamp_source="fetch_time")

    manager = ProviderManager(
        _FailingEast(), _FakeTencent(), attempts_per_provider=1,
    )
    tencent = manager.providers["tencent"]
    service = IntradayMarketDataService(manager)
    snapshot = await service.get_quote_snapshot("000001", as_of=NOW)
    assert snapshot.source == "tencent"
    assert tencent.calls == 1

@pytest.mark.asyncio
async def test_future_known_at_quote_marked_untrusted():
    """R4-P0-001 §26.1：known_at > as_of 的 Quote 绝不冒充新鲜价——
    显式降级 stale + UNTRUSTED + 原因。"""
    from app.v3.domain.intraday import IntradayQuoteSnapshot

    class _FutureProvider:
        async def get_quote(self, code: str) -> Quote:
            return _quote(
                source_timestamp=NOW + timedelta(hours=2),
                data_timestamp=NOW + timedelta(hours=2),
                server_timestamp=NOW + timedelta(hours=2),
            )

    service = IntradayMarketDataService(_FutureProvider())
    snapshot = await service.get_quote_snapshot("000001", as_of=NOW)
    assert snapshot.stale is True
    assert snapshot.quality == "UNTRUSTED"
    assert snapshot.stale_reason == "FUTURE_KNOWN_AT"
    assert isinstance(snapshot, IntradayQuoteSnapshot)


@pytest.mark.asyncio
async def test_future_event_time_quote_marked_untrusted():
    """R4-P0-001：event_time > as_of 同样降级（known_at 正常）。"""
    from app.v3.domain.intraday import IntradayQuoteSnapshot

    class _FutureEventProvider:
        async def get_quote(self, code: str) -> Quote:
            return _quote(data_timestamp=NOW + timedelta(minutes=30))

    service = IntradayMarketDataService(_FutureEventProvider())
    snapshot = await service.get_quote_snapshot("000001", as_of=NOW)
    assert snapshot.stale is True
    assert snapshot.quality == "UNTRUSTED"
    assert snapshot.stale_reason == "FUTURE_EVENT_TIME"
    assert isinstance(snapshot, IntradayQuoteSnapshot)
