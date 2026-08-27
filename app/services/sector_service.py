from __future__ import annotations

from app.models import SectorRanking
from app.providers.base import MarketDataProvider


class SectorService:
    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider

    async def get_sector_ranking(self, sector_type: str = "industry", limit: int = 30) -> SectorRanking:
        return await self.provider.get_sector_ranking(sector_type, limit)
