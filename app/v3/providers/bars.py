from __future__ import annotations

from typing import Protocol

from app.v3.domain.market_data import AdjustType, BarPeriod, HistoricalBarFetchResult


class HistoricalBarProviderError(RuntimeError):
    pass


class HistoricalBarProvider(Protocol):
    code: str

    async def fetch(
        self, code: str, period: BarPeriod, adjust_type: AdjustType, limit: int
    ) -> HistoricalBarFetchResult: ...

    async def close(self) -> None: ...
