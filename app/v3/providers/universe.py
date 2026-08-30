from __future__ import annotations

from typing import Protocol

from app.v3.domain.market_data import UniverseFetchResult


class UniverseProviderError(RuntimeError):
    pass


class UniverseProvider(Protocol):
    code: str

    async def fetch_snapshot(self) -> UniverseFetchResult: ...

    async def close(self) -> None: ...
