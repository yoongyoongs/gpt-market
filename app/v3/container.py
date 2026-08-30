from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.v3.infrastructure.db.session import V3Database
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork


@dataclass
class V3Container:
    enabled: bool
    database: V3Database | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "V3Container":
        if not settings.v3_enabled:
            return cls(enabled=False)
        if not settings.v3_database_url:
            raise ValueError("V3_DATABASE_URL is required when V3_ENABLED=true")
        return cls(
            enabled=True,
            database=V3Database(
                settings.v3_database_url,
                echo=settings.v3_database_echo,
                pool_size=settings.v3_database_pool_size,
                max_overflow=settings.v3_database_max_overflow,
            ),
        )

    async def start(self) -> None:
        if self.database is not None:
            await self.database.check_connection()

    async def close(self) -> None:
        if self.database is not None:
            await self.database.close()

    def uow(self) -> SQLAlchemyUnitOfWork:
        if self.database is None:
            raise RuntimeError("V3 database is disabled")
        return SQLAlchemyUnitOfWork(self.database.sessions)
