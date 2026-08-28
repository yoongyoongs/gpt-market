from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, fields
from datetime import datetime, time as datetime_time
from typing import Any

from app.config import Settings
from app.kline_cache import CachedKlineSeries, KlineCache
from app.models import Kline, KlineResult, Quote, SectorRanking
from app.providers.base import MarketDataProvider
from app.providers.manager import ProviderManager
from app.services.data_quality import DataQualityService
from app.utils.time import SHANGHAI, now_shanghai


@dataclass
class KlineMetrics:
    required: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    network_fetch: int = 0
    success: int = 0
    failed: int = 0
    stale_used: int = 0
    provisional_used: int = 0

    def as_dict(self) -> dict[str, int]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


def metric_delta(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in after}


def _is_trading_time(value: datetime) -> bool:
    local = value.astimezone(SHANGHAI)
    if local.weekday() >= 5:
        return False
    current = local.time()
    return datetime_time(9, 30) <= current <= datetime_time(11, 30) or datetime_time(13, 0) <= current <= datetime_time(15, 0)


def _should_use_provisional(value: datetime) -> bool:
    local = value.astimezone(SHANGHAI)
    return local.weekday() < 5 and datetime_time(9, 15) <= local.time() < datetime_time(15, 10)


def _merge_klines(*groups: list[Kline], limit: int) -> list[Kline]:
    by_date = {item.timestamp.date(): item for group in groups for item in group}
    return [by_date[date] for date in sorted(by_date)][-limit:]


def _provisional_bar(quote: Quote) -> Kline | None:
    required = (quote.open, quote.high, quote.low, quote.price, quote.volume, quote.amount)
    if any(value is None for value in required):
        return None
    return Kline(
        timestamp=datetime.combine(quote.server_timestamp.date(), datetime_time.min, tzinfo=SHANGHAI),
        open=quote.open,
        high=quote.high,
        low=quote.low,
        close=quote.price,
        volume=quote.volume,
        amount=quote.amount,
        provisional=True,
    )


class MarketDataService(MarketDataProvider):
    """Only business-facing market data dependency; owns stable K-line reads."""

    def __init__(
        self,
        providers: ProviderManager,
        cache: KlineCache,
        quality: DataQualityService,
        settings: Settings,
    ) -> None:
        self.providers = providers
        self.cache = cache
        self.quality = quality
        self.settings = settings
        self._kline_semaphore = asyncio.Semaphore(settings.max_kline_concurrency)
        self._key_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._key_guard = asyncio.Lock()
        self._metrics = KlineMetrics()
        self._recent_errors: Counter[str] = Counter()

    async def start(self) -> None:
        await self.cache.start()
        await self.providers.start()

    async def close(self) -> None:
        await self.providers.close()

    def health(self) -> dict[str, Any]:
        return {
            "providers": self.providers.health(),
            "kline": self.metrics_snapshot(),
            "l1_entries": self.cache.memory_entries(),
            "recent_kline_errors": dict(self._recent_errors.most_common(10)),
        }

    def metrics_snapshot(self) -> dict[str, int]:
        return self._metrics.as_dict()

    async def get_quote(self, code: str) -> Quote:
        return await self.providers.get_quote(code)

    async def get_index_quote(self, code: str, market: str) -> Quote:
        return await self.providers.get_index_quote(code, market)

    async def get_quotes(self, codes: list[str]) -> list[Quote]:
        return await self.providers.get_quotes(codes)

    async def get_all_a_shares(self) -> tuple[int, list[Quote]]:
        return await self.providers.get_all_a_shares()

    async def get_sector_ranking(self, sector_type: str, limit: int) -> SectorRanking:
        return await self.providers.get_sector_ranking(sector_type, limit)

    async def _key_lock(self, key: tuple[str, str, str]) -> asyncio.Lock:
        async with self._key_guard:
            return self._key_locks.setdefault(key, asyncio.Lock())

    def _cache_fresh(self, cached: CachedKlineSeries, now: datetime) -> bool:
        threshold = (
            self.settings.kline_refresh_trading_seconds
            if _is_trading_time(now)
            else self.settings.kline_refresh_closed_seconds
        )
        return (now - cached.updated_at).total_seconds() <= threshold

    async def get_kline(
        self, code: str, period: str, limit: int, adjust: str = "qfq", *, quote: Quote | None = None
    ) -> KlineResult:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if adjust not in {"qfq", "raw", "hfq"}:
            raise ValueError("adjust must be qfq, raw or hfq")
        self._metrics.required += 1
        if period not in {"day", "week", "month"}:
            self._metrics.network_fetch += 1
            try:
                async with self._kline_semaphore:
                    result = await self.providers.get_kline(code, period, limit, adjust)
                self._metrics.success += 1
                return result
            except Exception as exc:
                self._record_error(exc)
                self._metrics.failed += 1
                raise

        key = (code, period, adjust)
        lock = await self._key_lock(key)
        async with lock:
            now = now_shanghai()
            cached = await self.cache.get(code, period, adjust, limit)
            has_provisional = (
                period == "day" and
                quote is not None
                and quote.server_timestamp.date() == now.date()
                and _should_use_provisional(now)
            )
            historical_needed = max(1, limit - (1 if has_provisional else 0))
            cache_usable = cached is not None and len(cached.klines) >= historical_needed
            if cache_usable and self._cache_fresh(cached, now):
                self._metrics.cache_hit += 1
                self._metrics.success += 1
                return self._result_from_cache(code, period, limit, cached, quote, cache_hit=True, stale=False)

            self._metrics.cache_miss += 1
            self._metrics.network_fetch += 1
            network_limit = limit if cached is None or not cache_usable else min(limit, 10)
            try:
                async with self._kline_semaphore:
                    network = await self.providers.get_kline(code, period, network_limit, adjust)
                formal = self._formal_bars_for_persistence(network.klines, now, period)
                if formal:
                    await self.cache.put(code, period, adjust, formal, network.source, now)
                combined = _merge_klines(
                    cached.klines if cached else [],
                    network.klines,
                    limit=limit,
                )
                cache_view = CachedKlineSeries(combined, network.source, now)
                self._metrics.success += 1
                return self._result_from_cache(code, period, limit, cache_view, quote, cache_hit=False, stale=False)
            except Exception as exc:
                self._record_error(exc)
                if cache_usable and cached is not None:
                    self._metrics.stale_used += 1
                    self._metrics.success += 1
                    return self._result_from_cache(code, period, limit, cached, quote, cache_hit=True, stale=True)
                self._metrics.failed += 1
                raise

    def _record_error(self, error: Exception) -> None:
        key = f"{type(error).__name__}:{str(error)[:300]}"
        self._recent_errors[key] += 1

    @staticmethod
    def _formal_bars_for_persistence(klines: list[Kline], now: datetime, period: str = "day") -> list[Kline]:
        if period != "day":
            return klines
        after_close = now.time() >= datetime_time(15, 10)
        return [item for item in klines if item.timestamp.date() < now.date() or after_close]

    def _result_from_cache(
        self,
        code: str,
        period: str,
        limit: int,
        cached: CachedKlineSeries,
        quote: Quote | None,
        *,
        cache_hit: bool,
        stale: bool,
    ) -> KlineResult:
        klines = list(cached.klines)
        timestamp = cached.updated_at
        timestamp_source = "fetch_time"
        source = f"cache:{cached.source}" if cache_hit else cached.source
        current = now_shanghai()
        if (
            period == "day" and
            quote is not None
            and quote.server_timestamp.date() == current.date()
            and _should_use_provisional(current)
        ):
            provisional = _provisional_bar(quote)
            if provisional is not None:
                klines = _merge_klines(klines, [provisional], limit=limit)
                timestamp = quote.source_timestamp
                timestamp_source = quote.timestamp_source
                source = f"{source}+{quote.source}"
                self._metrics.provisional_used += 1
        quality = self.quality.assess(
            timestamp,
            timestamp_source=timestamp_source,
            source=source,
            complete=len(klines) >= min(limit, 20),
            server_timestamp=current,
        )
        if stale:
            quality.update(stale=True, quality="OLD", confidence="LOW")
        return KlineResult(code=code, period=period, klines=klines[-limit:], **quality)
