from __future__ import annotations

from datetime import date
from typing import Protocol

from app.v3.domain.market_data import CorporateActionFetchResult


class CorporateActionProviderError(RuntimeError):
    pass


class CorporateActionProvider(Protocol):
    code: str

    async def fetch_since(self, since: date) -> CorporateActionFetchResult: ...

    async def close(self) -> None: ...
