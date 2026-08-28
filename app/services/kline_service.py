from __future__ import annotations

import asyncio

from app.cache import AsyncTTLCache
from app.models import KlineResult, StockDetail
from app.providers.base import MarketDataProvider
from app.services.technical_indicator_service import TechnicalIndicatorService


class KlineService:
    def __init__(
        self,
        provider: MarketDataProvider,
        cache: AsyncTTLCache,
        indicators: TechnicalIndicatorService,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.indicators = indicators

    async def get_kline(self, code: str, period: str, limit: int) -> KlineResult:
        return await self.provider.get_kline(code, period, limit)

    async def get_stock_detail(self, code: str) -> StockDetail:
        async def load() -> StockDetail:
            quote = await self.provider.get_quote(code)
            day, minute = await asyncio.gather(
                self.provider.get_kline(code, "day", 120, "qfq", quote=quote),
                self.provider.get_kline(code, "5m", 48),
                return_exceptions=True,
            )
            if isinstance(day, Exception):
                raise day
            minute_klines = [] if isinstance(minute, Exception) else minute.klines
            technical = self.indicators.calculate(day.klines, quote.price)
            return StockDetail(
                quote=quote,
                technical=technical,
                day_klines=day.klines,
                minute_5_klines=minute_klines,
                source=quote.source,
                source_timestamp=quote.source_timestamp,
                data_timestamp=quote.data_timestamp,
                server_timestamp=quote.server_timestamp,
                age_seconds=quote.age_seconds,
                stale=quote.stale,
                quality=quote.quality,
                timestamp_source=quote.timestamp_source,
                snapshot_id=quote.snapshot_id,
                confidence=quote.confidence,
            )

        return await self.cache.get_or_set(f"detail:{code}", 3, load)
