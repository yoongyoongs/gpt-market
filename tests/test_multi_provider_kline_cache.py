from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import Settings
from app.kline_cache import KlineCache
from app.models import Kline, KlineResult, Quote
from app.providers.base import AllProvidersFailedError, ProviderEmptyDataError, ProviderUnsupportedError
from app.providers.manager import ProviderManager
from app.providers.tencent import parse_tencent_kline_rows, parse_tencent_quote, to_tencent_symbol
from app.services.data_quality import DataQualityService
from app.services.market_data_service import MarketDataService
from app.utils.time import now_shanghai


QUALITY = DataQualityService()


def quote(code: str = "603019", source: str = "eastmoney") -> Quote:
    now = now_shanghai()
    return Quote(
        code=code, name="fixture", market="SH" if code.startswith("6") else "SZ",
        price=10.5, prev_close=10.0, open=10.1, high=10.8, low=9.9,
        pct_change=5.0, change=0.5, volume=1_000_000, amount=10_500_000,
        turnover_rate=2.0, volume_ratio=1.2, amplitude=9.0,
        **QUALITY.assess(now, timestamp_source="fetch_time", source=source, server_timestamp=now),
    )


def kline_result(code: str = "603019", source: str = "tencent") -> KlineResult:
    now = now_shanghai()
    rows = [
        Kline(timestamp=now - timedelta(days=offset), open=9, high=11, low=8, close=10, volume=1000, amount=10000)
        for offset in (3, 2, 1)
    ]
    return KlineResult(
        code=code, period="day", klines=rows,
        **QUALITY.assess(now, timestamp_source="fetch_time", source=source, server_timestamp=now),
    )


class StubProvider:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.kline_calls = 0

    async def start(self): pass
    async def close(self): pass

    async def get_quote(self, code):
        if self.fail:
            raise ProviderEmptyDataError(f"{self.name} empty quote")
        return quote(code, self.name)

    async def get_quotes(self, codes):
        return [await self.get_quote(code) for code in codes]

    async def get_index_quote(self, code, market):
        return await self.get_quote(code)

    async def get_kline(self, code, period, limit, adjust="qfq", *, quote=None):
        self.kline_calls += 1
        if self.fail:
            raise ProviderEmptyDataError(f"{self.name} empty klines")
        return kline_result(code, self.name)

    async def get_all_a_shares(self):
        raise ProviderUnsupportedError("not needed")

    async def get_sector_ranking(self, sector_type, limit):
        raise ProviderUnsupportedError("not needed")


def test_tencent_symbol_quote_and_qfq_parsing() -> None:
    assert to_tencent_symbol("603019") == "sh603019"
    assert to_tencent_symbol("002284") == "sz002284"
    values = [""] * 70
    values[1:7] = ["中科曙光", "603019", "87.17", "86.54", "86.60", "80526"]
    values[30:35] = ["20260828095546", "0.63", "0.73", "87.35", "85.80"]
    values[35] = "87.17/80526/699251925"
    values[38] = "0.55"
    values[43] = "1.79"
    parsed = parse_tencent_quote(f'v_sh603019="{"~".join(values)}";', now_shanghai(), QUALITY)
    assert parsed.code == "603019"
    assert parsed.price == 87.17
    assert parsed.volume == 8_052_600
    assert parsed.amount == 699_251_925
    assert parsed.source == "tencent"

    klines = parse_tencent_kline_rows([["2026-08-27", "85", "86.54", "87.26", "84.9", "382771"]])
    assert klines[0].close == 86.54
    assert klines[0].volume == 38_277_100


async def test_provider_manager_falls_back_and_tracks_health() -> None:
    eastmoney = StubProvider("eastmoney", fail=True)
    tencent = StubProvider("tencent")
    manager = ProviderManager(eastmoney, tencent, attempts_per_provider=1)
    result = await manager.get_kline("603019", "day", 80)
    assert result.source == "tencent"
    health = manager.health()
    assert health["eastmoney"]["failure_count"] == 1
    assert health["eastmoney"]["empty_data_count"] == 1
    assert health["eastmoney"]["last_error_category"] == "EMPTY_DATA"
    assert "empty klines" in health["eastmoney"]["last_error"]
    assert health["tencent"]["success_count"] == 1


async def test_both_providers_fail_without_cache_is_unavailable(tmp_path) -> None:
    eastmoney = StubProvider("eastmoney", fail=True)
    tencent = StubProvider("tencent", fail=True)
    manager = ProviderManager(eastmoney, tencent, attempts_per_provider=1)
    path = tmp_path / "empty.sqlite3"
    settings = Settings(kline_cache_path=str(path), max_kline_concurrency=5)
    service = MarketDataService(manager, KlineCache(str(path)), QUALITY, settings)
    await service.start()
    with pytest.raises(AllProvidersFailedError, match="ALL_PROVIDER_FAILED"):
        await service.get_kline("603019", "day", 80, "qfq", quote=quote())
    assert service.metrics_snapshot()["failed"] == 1
    assert service.health()["recent_kline_errors"]
    await service.close()


async def test_kline_l1_l2_cache_provisional_and_stale_fallback(tmp_path, monkeypatch) -> None:
    fixed_now = now_shanghai().replace(hour=10, minute=0, second=0, microsecond=0)
    clock = [fixed_now]
    monkeypatch.setattr("app.services.market_data_service.now_shanghai", lambda: clock[0])
    eastmoney = StubProvider("eastmoney", fail=True)
    tencent = StubProvider("tencent")
    manager = ProviderManager(eastmoney, tencent, attempts_per_provider=1)
    path = tmp_path / "klines.sqlite3"
    settings = Settings(
        kline_cache_path=str(path),
        kline_refresh_trading_seconds=3600,
        kline_refresh_closed_seconds=3600,
        max_kline_concurrency=5,
    )
    cache = KlineCache(str(path))
    service = MarketDataService(manager, cache, QUALITY, settings)
    await service.start()
    current_quote = quote()

    first = await service.get_kline("603019", "day", 3, "qfq", quote=current_quote)
    calls_after_first = tencent.kline_calls
    second = await service.get_kline("603019", "day", 3, "qfq", quote=current_quote)
    assert len(first.klines) == 3
    assert first.klines[-1].close == current_quote.price
    assert tencent.kline_calls == calls_after_first
    assert service.metrics_snapshot()["cache_hit"] == 1

    # A fresh process loads formal bars from SQLite; today's provisional quote was not persisted.
    reloaded = KlineCache(str(path))
    await reloaded.start()
    disk = await reloaded.get("603019", "day", "qfq", 3)
    assert disk is not None
    assert all(item.timestamp.date() < now_shanghai().date() for item in disk.klines)

    # Force refresh, fail both providers, and verify old cache is returned rather than cleared.
    settings.kline_refresh_trading_seconds = 0
    settings.kline_refresh_closed_seconds = 0
    clock[0] += timedelta(seconds=1)
    tencent.fail = True
    stale = await service.get_kline("603019", "day", 3, "qfq", quote=current_quote)
    assert stale.stale is True
    assert stale.quality == "OLD"
    assert service.metrics_snapshot()["stale_used"] == 1
    await service.close()
