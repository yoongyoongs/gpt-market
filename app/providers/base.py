from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import KlineResult, Quote, SectorRanking


class ProviderError(RuntimeError):
    pass


class MarketDataProvider(ABC):
    @abstractmethod
    async def get_quote(self, code: str) -> Quote: ...

    @abstractmethod
    async def get_index_quote(self, code: str, market: str) -> Quote: ...

    @abstractmethod
    async def get_quotes(self, codes: list[str]) -> list[Quote]: ...

    @abstractmethod
    async def get_kline(self, code: str, period: str, limit: int) -> KlineResult: ...

    @abstractmethod
    async def get_all_a_shares(self) -> tuple[int, list[Quote]]: ...

    @abstractmethod
    async def get_sector_ranking(self, sector_type: str, limit: int) -> SectorRanking: ...
