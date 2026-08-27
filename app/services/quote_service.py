from __future__ import annotations

from app.models import Quote
from app.providers.base import MarketDataProvider


class QuoteService:
    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider

    async def get_quote(self, code: str) -> Quote:
        return await self.provider.get_quote(code)

    async def get_quotes(self, codes: list[str]) -> list[Quote]:
        if not codes:
            raise ValueError("codes must not be empty")
        return await self.provider.get_quotes(codes)
