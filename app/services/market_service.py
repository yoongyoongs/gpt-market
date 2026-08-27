from __future__ import annotations

import asyncio

from app.cache import AsyncTTLCache
from app.models import AvailableValue, IndexSnapshot, MarketBreadth, MarketOverview
from app.providers.base import MarketDataProvider
from app.services.data_quality import DataQualityService

INDEX_CODES = {"shanghai": ("000001", "SH"), "shenzhen": ("399001", "SZ"), "chinext": ("399006", "SZ")}


class MarketService:
    def __init__(self, provider: MarketDataProvider, cache: AsyncTTLCache, quality: DataQualityService) -> None:
        self.provider = provider
        self.cache = cache
        self.quality = quality

    async def get_market_overview(self) -> MarketOverview:
        async def load() -> MarketOverview:
            list_task = self.provider.get_all_a_shares()
            index_results = await asyncio.gather(
                *(self.provider.get_index_quote(code, market) for code, market in INDEX_CODES.values()), return_exceptions=True
            )
            total, shares = await list_task
            indices: dict[str, IndexSnapshot] = {}
            for (key, (code, _market)), result in zip(INDEX_CODES.items(), index_results, strict=True):
                if isinstance(result, Exception):
                    indices[key] = IndexSnapshot(code=code, name=key, price=None, pct_change=None)
                else:
                    indices[key] = IndexSnapshot(code=code, name=result.name, price=result.price, pct_change=result.pct_change)
            up = sum(1 for item in shares if item.pct_change is not None and item.pct_change > 0)
            down = sum(1 for item in shares if item.pct_change is not None and item.pct_change < 0)
            flat = sum(1 for item in shares if item.pct_change == 0)
            timestamps = [item.data_timestamp for item in shares]
            timestamps.extend(item.data_timestamp for item in index_results if not isinstance(item, Exception))
            from app.utils.time import now_shanghai
            timestamp = max(timestamps, default=now_shanghai())
            return MarketOverview(
                indices=indices,
                breadth=MarketBreadth(
                    up_count=up, down_count=down, flat_count=flat,
                    limit_up_count=AvailableValue(value=None, available=False),
                    limit_down_count=AvailableValue(value=None, available=False),
                ),
                amount=sum(item.amount or 0 for item in shares),
                **self.quality.assess(timestamp, complete=bool(shares)),
            )

        return await self.cache.get_or_set("market:latest", 5, load)
