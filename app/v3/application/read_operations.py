"""RC-08B READ Contract 服务（API-002）：READ 只投影 immutable 事实，
不做任何写副作用、不聚合出无事实依据的分数。"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID


class ReadOperationsService:
    def __init__(self, uow_factory: Callable) -> None:
        self._uow_factory = uow_factory

    async def portfolio_overview(self, limit: int = 100) -> dict:
        async with self._uow_factory() as uow:
            return await uow.reads.portfolio_overview(limit)

    async def position_reviews_by_code(self, code: str, limit: int = 100) -> list:
        async with self._uow_factory() as uow:
            return await uow.reads.position_reviews_by_code(code, limit)

    async def adjustments_by_code(self, code: str, limit: int = 100) -> list:
        async with self._uow_factory() as uow:
            return await uow.reads.adjustments_by_code(code, limit)

    async def preferences(self, limit: int = 100) -> list:
        async with self._uow_factory() as uow:
            return await uow.reads.preferences(limit)

    async def entry_plan_versions(self, entry_plan_id: UUID) -> list:
        async with self._uow_factory() as uow:
            return await uow.reads.entry_plan_versions(entry_plan_id)

    async def watchlist_changes(self, limit: int = 100) -> list:
        async with self._uow_factory() as uow:
            return await uow.reads.watchlist_changes(limit)

    async def decisions(self, limit: int = 100) -> list:
        async with self._uow_factory() as uow:
            return await uow.reads.decisions(limit)

    async def reviews(self, limit: int = 100) -> list:
        async with self._uow_factory() as uow:
            return await uow.reads.reviews(limit)

    async def market_reviews(self, limit: int = 100) -> list:
        async with self._uow_factory() as uow:
            return await uow.reads.market_reviews(limit)

    async def performance(self, limit: int = 100) -> dict:
        async with self._uow_factory() as uow:
            return await uow.reads.performance(limit)

    async def data_quality(self) -> dict:
        async with self._uow_factory() as uow:
            return await uow.reads.data_quality()
