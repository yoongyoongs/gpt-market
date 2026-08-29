from __future__ import annotations

from app.cache import AsyncTTLCache
from app.config import get_settings
from app.providers.eastmoney import EastmoneyProvider
from app.providers.tencent import TencentProvider
from app.providers.manager import ProviderManager
from app.kline_cache import KlineCache
from app.services.kline_service import KlineService
from app.services.market_service import MarketService
from app.services.quote_service import QuoteService
from app.services.scanner import ScannerService
from app.services.sector_service import SectorService
from app.services.data_quality import DataQualityService
from app.services.technical_indicator_service import TechnicalIndicatorService
from app.services.market_data_service import MarketDataService
from app.fundamentals.eastmoney import EastmoneyDatacenterFundamentalProvider, EastmoneyF10FundamentalProvider
from app.fundamentals.manager import FundamentalProviderManager
from app.v3.container import V3Container


class Container:
    def __init__(self) -> None:
        settings = get_settings()
        self.cache = AsyncTTLCache()
        self.data_quality = DataQualityService(
            settings.stale_after_seconds, settings.old_after_seconds, settings.unavailable_after_seconds
        )
        self.technical_indicators = TechnicalIndicatorService()
        # Raw providers remain transport adapters. Every business service receives
        # only the unified MarketDataService below.
        self.eastmoney = EastmoneyProvider(settings, self.cache, self.data_quality)
        self.tencent = TencentProvider(settings, self.data_quality)
        self.provider_manager = ProviderManager(self.eastmoney, self.tencent)
        self.kline_cache = KlineCache(settings.kline_cache_path)
        self.market_data = MarketDataService(
            self.provider_manager, self.kline_cache, self.data_quality, settings
        )
        # Compatibility alias for internal scripts; it no longer points at a raw provider.
        self.provider = self.market_data
        self.fundamental_primary = EastmoneyDatacenterFundamentalProvider(
            settings.fundamental_timeout, settings.eastmoney_proxy
        )
        self.fundamental_fallback = EastmoneyF10FundamentalProvider(
            settings.fundamental_timeout, settings.eastmoney_proxy
        )
        self.fundamentals = FundamentalProviderManager(
            self.fundamental_primary,
            [self.fundamental_fallback],
            cache=self.cache,
            ttl_seconds=settings.fundamental_cache_seconds,
        )
        self.quotes = QuoteService(self.market_data)
        self.klines = KlineService(self.market_data, self.cache, self.technical_indicators)
        self.market = MarketService(self.market_data, self.cache, self.data_quality)
        self.sectors = SectorService(self.market_data)
        self.scanner = ScannerService(
            self.market_data,
            self.cache,
            settings.scan_concurrency,
            self.technical_indicators,
            self.data_quality,
            self.fundamentals,
        )
        self.v3 = V3Container.from_settings(settings)

    async def start(self) -> None:
        await self.market_data.start()
        await self.v3.start()

    async def close(self) -> None:
        await self.v3.close()
        await self.fundamentals.close()
        await self.market_data.close()


container = Container()
