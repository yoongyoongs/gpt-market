from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models import KlineResult, Quote, SectorRanking


class ProviderError(RuntimeError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderEmptyDataError(ProviderError):
    pass


class ProviderParseError(ProviderError):
    pass


class ProviderUnsupportedError(ProviderError):
    pass


class AllProvidersFailedError(ProviderError):
    pass


def provider_error_category(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, AllProvidersFailedError):
        return "ALL_PROVIDER_FAILED"
    if isinstance(exc, ProviderEmptyDataError):
        return "EMPTY_DATA"
    if isinstance(exc, (ProviderTimeoutError, TimeoutError)):
        return "TIMEOUT"
    if isinstance(exc, ProviderParseError):
        return "PARSE_ERROR"
    if isinstance(exc, ProviderUnsupportedError):
        return "UNSUPPORTED"
    if isinstance(exc, ValueError):
        return "INVALID_SYMBOL" if "code" in text or "symbol" in text else "INVALID_DATA"
    if "429" in text or "rate limit" in text:
        return "RATE_LIMIT"
    if "http" in text:
        return "HTTP_ERROR"
    return "NETWORK_ERROR"


class MarketDataProvider(ABC):
    @abstractmethod
    async def get_quote(self, code: str) -> Quote: ...

    @abstractmethod
    async def get_index_quote(self, code: str, market: str) -> Quote: ...

    @abstractmethod
    async def get_quotes(self, codes: list[str]) -> list[Quote]: ...

    @abstractmethod
    async def get_kline(
        self, code: str, period: str, limit: int, adjust: str = "qfq", *, quote: Quote | None = None
    ) -> KlineResult: ...

    @abstractmethod
    async def get_all_a_shares(self) -> tuple[int, list[Quote]]: ...

    @abstractmethod
    async def get_sector_ranking(self, sector_type: str, limit: int) -> SectorRanking: ...

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {}
