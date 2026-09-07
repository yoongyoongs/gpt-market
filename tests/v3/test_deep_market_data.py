from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.v3.application.deep_market_data import DeepMarketDataService


NOW = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)
AS_OF = NOW - timedelta(hours=1)


class FakeMinuteProvider:
    def __init__(self, periods_with_bars: dict[str, int] = {"5m": 20, "15m": 16, "60m": 12}):
        self.periods = dict(periods_with_bars)
        self.calls: list[tuple[str, str]] = []

    async def get_kline(self, code: str, period: str, limit: int, adjust: str = "qfq"):
        self.calls.append((code, period))
        count = self.periods.get(period, 0)
        bars = [
            type("Bar", (), {
                "timestamp": NOW - timedelta(minutes=(count - i) * 5),
                "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1,
                "volume": 1000, "amount": 1_000_000.0, "provisional": False,
            })()
            for i in range(count)
        ]
        result = type("Result", (), {
            "klines": bars, "stale": False,
            "source": "eastmoney", "data_timestamp": AS_OF,
        })()
        return result


def make_service(provider) -> DeepMarketDataService:
    return DeepMarketDataService(provider, clock=lambda: NOW)


@pytest.mark.asyncio
async def test_intraday_structure_covers_all_periods_with_point_in_time_metadata() -> None:
    provider = FakeMinuteProvider()
    service = make_service(provider)
    structure = await service.get_intraday_structure(
        "000001", as_of=NOW + timedelta(minutes=1),
    )
    assert structure.code == "000001"
    assert structure.as_of == NOW + timedelta(minutes=1)
    assert structure.known_at == NOW
    assert structure.source == "eastmoney"
    assert set(structure.periods) == {"5m", "15m", "60m"}
    for period, item in structure.periods.items():
        assert item["status"] == "AVAILABLE", (period, item)
        assert item["precision"] == "LIMITED"
        assert item["reason"] == "MINUTE_FACTS_ARE_FETCH_TIME_FACTS"
        assert item["bar_count"] == FakeMinuteProvider().periods[period]
        assert item["stale"] is False
    assert ("000001", "5m") in provider.calls


@pytest.mark.asyncio
async def test_bars_after_as_of_are_not_presented_as_history() -> None:
    provider = FakeMinuteProvider({"5m": 4, "15m": 0, "60m": 0})
    service = make_service(provider)
    # as_of 晚于全部 bar：正常
    structure = await service.get_intraday_structure(
        "000001", as_of=NOW + timedelta(hours=1),
    )
    assert structure.periods["5m"]["status"] == "AVAILABLE"
    # as_of 早于全部 bar：不能伪装历史，显式 UNKNOWN
    early = await service.get_intraday_structure(
        "000001", as_of=NOW - timedelta(days=30),
    )
    assert early.periods["5m"]["status"] == "UNKNOWN"
    assert early.periods["5m"]["reason"] == "NO_BARS_KNOWN_AT_AS_OF"
    # provider 未返回该周期数据：显式 UNKNOWN
    missing = structure.periods["15m"]
    assert missing["status"] == "UNKNOWN"
    assert missing["reason"] == "NO_BARS_KNOWN_AT_AS_OF"


@pytest.mark.asyncio
async def test_provider_failure_is_isolated_per_period() -> None:
    class FailingProvider:
        async def get_kline(self, code, period, limit, adjust="qfq"):
            if period == "15m":
                raise RuntimeError("upstream down")
            return type("Result", (), {
                "klines": [type("Bar", (), {
                    "timestamp": NOW, "open": 10, "high": 10, "low": 9.9,
                    "close": 10.1, "volume": 1, "amount": 1.0,
                    "provisional": False,
                })()],
                "stale": False,
            })()

    service = make_service(FailingProvider())
    structure = await service.get_intraday_structure("000001", as_of=NOW)
    assert structure.periods["15m"]["status"] == "UNKNOWN"
    assert "RuntimeError" in structure.periods["15m"]["reason"]
    assert structure.periods["5m"]["status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_sixty_minute_structure_returns_trend_support_resistance() -> None:
    """§14.1：60m 必须给出结构/支撑/压力/趋势；数据不足显式 UNKNOWN。"""

    class RisingProvider:
        async def get_kline(self, code, period, limit, adjust="qfq"):
            count = 12
            bars = [
                type("Bar", (), {
                    "timestamp": NOW - timedelta(minutes=(count - i) * 60),
                    "open": 10.0 + i * 0.1, "high": 10.3 + i * 0.1,
                    "low": 9.8 + i * 0.1, "close": 10.1 + i * 0.1,
                    "volume": 1000, "amount": 1_000_000.0,
                    "provisional": False,
                })()
                for i in range(count)
            ]
            return type("Result", (), {"klines": bars, "stale": False})()

    structure = await make_service(RisingProvider()).get_intraday_structure(
        "000001", as_of=NOW,
    )
    sixty = structure.periods["60m"]
    assert sixty["status"] == "AVAILABLE"
    structure_60m = sixty["structure"]
    assert structure_60m["trend"] == "UP"
    assert structure_60m["support"] == pytest.approx(min(9.8 + i * 0.1 for i in range(12)))
    assert structure_60m["resistance"] == pytest.approx(max(10.3 + i * 0.1 for i in range(12)))

    # 数据不足：结构显式 UNKNOWN，绝不给伪支撑/压力
    structure_short = await make_service(FakeMinuteProvider({
        "5m": 3, "15m": 0, "60m": 0,
    })).get_intraday_structure("000001", as_of=NOW)
    short = structure_short.periods["5m"]
    assert short["status"] == "AVAILABLE"
    assert short["structure"]["trend"] == "UNKNOWN"
    assert short["structure"]["support"] is None
    assert short["structure"]["resistance"] is None


@pytest.mark.asyncio
async def test_flat_structure_is_sideways_per_unified_vocabulary() -> None:
    """RT-02：词表统一——横盘一律 SIDEWAYS（实时方案 §4.4），不再用 RANGE。"""

    class FlatProvider:
        async def get_kline(self, code, period, limit, adjust="qfq"):
            return type("Result", (), {
                "klines": [
                    type("Bar", (), {
                        "timestamp": NOW - timedelta(minutes=5 * i),
                        "open": 10.0, "high": 10.01, "low": 9.99,
                        "close": 10.0, "volume": 1, "amount": 1.0,
                        "provisional": False,
                    })
                    for i in range(12)
                ],
                "stale": False, "source": "tencent",
            })()

    structure = await make_service(FlatProvider()).get_intraday_structure(
        "600000", as_of=NOW,
    )
    assert structure.periods["60m"]["structure"]["trend"] == "SIDEWAYS"


@pytest.mark.asyncio
async def test_deep_period_event_time_is_upstream_data_timestamp() -> None:
    """FC-04 阻断点 5：Deep period event_time = 上游 data_timestamp，
    与 Quote/Kline provenance 全链统一；first/last_bar_time 是窗口边界，
    不是同一 Contract 字段。"""
    provider = FakeMinuteProvider()
    service = make_service(provider)
    structure = await service.get_intraday_structure(
        "000001", as_of=NOW + timedelta(minutes=1),
    )
    for period, item in structure.periods.items():
        assert item["status"] == "AVAILABLE", (period, item)
        assert item["event_time"] == AS_OF
        assert item["upstream_source"] == "eastmoney"
        assert item["fallback_used"] is False
        assert item["known_at"] == NOW  # fetch 完成后取时点


@pytest.mark.asyncio
async def test_deep_period_failure_reports_event_time_none() -> None:
    """FC-04：provider 失败路径 event_time 显式 None，不伪造上游时点。"""
    from app.v3.application.deep_market_data import DeepMarketDataService

    class _BoomProvider:
        async def get_kline(self, code, period, limit, adjust="qfq"):
            raise RuntimeError("clist down")

    service = DeepMarketDataService(_BoomProvider(), clock=lambda: NOW)
    structure = await service.get_intraday_structure(
        "000001", as_of=AS_OF,
    )
    for period, item in structure.periods.items():
        assert item["status"] == "UNKNOWN"
        assert item["event_time"] is None
