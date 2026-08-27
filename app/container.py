from __future__ import annotations

from app.cache import AsyncTTLCache
from app.config import get_settings
from app.providers.eastmoney import EastmoneyProvider
from app.services.kline_service import KlineService
from app.services.market_service import MarketService
from app.services.quote_service import QuoteService
from app.services.scanner import ScannerService
from app.services.sector_service import SectorService
from app.services.data_quality import DataQualityService
from app.services.technical_indicator_service import TechnicalIndicatorService


class Container:
    def __init__(self) -> None:
        settings = get_settings()
        self.cache = AsyncTTLCache()
        self.data_quality = DataQualityService(
            settings.stale_after_seconds, settings.old_after_seconds, settings.unavailable_after_seconds
        )
        self.technical_indicators = TechnicalIndicatorService()
        self.provider = EastmoneyProvider(settings, self.cache, self.data_quality)
        self.quotes = QuoteService(self.provider)
        self.klines = KlineService(self.provider, self.cache, self.technical_indicators)
        self.market = MarketService(self.provider, self.cache, self.data_quality)
        self.sectors = SectorService(self.provider)
        self.scanner = ScannerService(
            self.provider, self.cache, settings.scan_concurrency, self.technical_indicators, self.data_quality
        )

    async def start(self) -> None:
        await self.provider.start()

    async def close(self) -> None:
        await self.provider.close()


container = Container()
