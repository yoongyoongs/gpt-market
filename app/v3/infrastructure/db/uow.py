from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.v3.infrastructure.db.repositories import (
    SQLAlchemyAgentTaskRepository,
    SQLAlchemyAuditRepository,
    SQLAlchemyBarRepository,
    SQLAlchemyCandidateComparisonRepository,
    SQLAlchemyContextPackRepository,
    SQLAlchemyCorporateActionRepository,
    SQLAlchemyIngestionRunRepository,
    SQLAlchemyFeatureRepository,
    SQLAlchemyEvidenceRepository,
    SQLAlchemyRecallRepository,
    SQLAlchemyUniverseRepository,
)


class SQLAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> "SQLAlchemyUnitOfWork":
        self._session = self._session_factory()
        self._committed = False
        self.tasks = SQLAlchemyAgentTaskRepository(self._session)
        self.audits = SQLAlchemyAuditRepository(self._session)
        self.universes = SQLAlchemyUniverseRepository(self._session)
        self.bars = SQLAlchemyBarRepository(self._session)
        self.corporate_actions = SQLAlchemyCorporateActionRepository(self._session)
        self.ingestion_runs = SQLAlchemyIngestionRunRepository(self._session)
        self.features = SQLAlchemyFeatureRepository(self._session)
        self.evidence = SQLAlchemyEvidenceRepository(self._session)
        self.recalls = SQLAlchemyRecallRepository(self._session)
        self.candidate_comparisons = SQLAlchemyCandidateComparisonRepository(
            self._session
        )
        self.context_packs = SQLAlchemyContextPackRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        try:
            if exc_type is not None or not self._committed:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        await self._session.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        await self._session.rollback()
