from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class V3Database:
    def __init__(self, url: str, *, echo: bool, pool_size: int, max_overflow: int) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max_overflow,
        )
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self._advisory_connection = None

    async def check_connection(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def acquire_advisory_lock(self, key: int) -> None:
        if self._advisory_connection is not None:
            raise RuntimeError("this database instance already holds an advisory lock")
        connection = await self.engine.connect()
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        )
        if not acquired:
            await connection.close()
            raise RuntimeError(f"V3 market-data job advisory lock {key} is already held")
        self._advisory_connection = connection

    async def close(self) -> None:
        if self._advisory_connection is not None:
            await self._advisory_connection.close()
            self._advisory_connection = None
        await self.engine.dispose()
