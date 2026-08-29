from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import FundamentalSnapshot


class FundamentalProviderError(RuntimeError):
    pass


class FundamentalProvider(ABC):
    name: str
    upstream_source: str

    @abstractmethod
    async def get_many(self, codes: list[str]) -> dict[str, FundamentalSnapshot]: ...

    async def close(self) -> None:
        return None
