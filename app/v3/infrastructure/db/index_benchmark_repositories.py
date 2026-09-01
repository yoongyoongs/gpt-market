from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.domain.index_benchmark import (
    IndexBenchmarkBar,
    IndexBenchmarkRevision,
    IndexBenchmarkRevisionContent,
    IndexBenchmarkStatus,
)
from app.v3.infrastructure.db.models import IndexBenchmarkRevisionModel


class SQLAlchemyIndexBenchmarkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish(self, revision: IndexBenchmarkRevision) -> bool:
        """append-only + 内容寻址去重：相同内容返回 False。"""
        existing = await self._session.scalar(
            select(IndexBenchmarkRevisionModel.revision_id).where(
                IndexBenchmarkRevisionModel.content_hash == revision.content_hash,
            )
        )
        if existing is not None:
            return False
        self._session.add(IndexBenchmarkRevisionModel(
            revision_id=revision.revision_id,
            benchmark_code=revision.benchmark_code,
            source=revision.source,
            upstream_source=revision.upstream_source,
            fetch_time=revision.fetch_time,
            known_at=revision.known_at,
            status=revision.status.value,
            bars=[bar.model_dump(mode="json") for bar in revision.bars],
            content_hash=revision.content_hash,
        ))
        await self._session.flush()
        return True

    async def latest(
        self, benchmark_code: str, *, as_of: datetime
    ) -> IndexBenchmarkRevision | None:
        """点时读取：known_at <= as_of 的最新 PUBLISHED revision。"""
        row = (
            await self._session.scalars(
                select(IndexBenchmarkRevisionModel)
                .where(
                    IndexBenchmarkRevisionModel.benchmark_code == benchmark_code,
                    IndexBenchmarkRevisionModel.status == "PUBLISHED",
                    IndexBenchmarkRevisionModel.known_at <= as_of,
                )
                .order_by(
                    IndexBenchmarkRevisionModel.known_at.desc(),
                    IndexBenchmarkRevisionModel.revision_id.desc(),
                )
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        return IndexBenchmarkRevision(
            revision_id=row.revision_id,
            benchmark_code=row.benchmark_code,
            source=row.source,
            upstream_source=row.upstream_source,
            fetch_time=row.fetch_time,
            known_at=row.known_at,
            status=IndexBenchmarkStatus(row.status),
            bars=tuple(
                IndexBenchmarkBar.model_validate(bar) for bar in (row.bars or [])
            ),
            content_hash=row.content_hash,
        )

    @staticmethod
    def from_model(row: IndexBenchmarkRevisionModel) -> IndexBenchmarkRevision:
        return IndexBenchmarkRevision.build(IndexBenchmarkRevisionContent(
            revision_id=row.revision_id,
            benchmark_code=row.benchmark_code,
            source=row.source,
            upstream_source=row.upstream_source,
            fetch_time=row.fetch_time,
            known_at=row.known_at,
            bars=tuple(
                IndexBenchmarkBar.model_validate(bar) for bar in (row.bars or [])
            ),
        ))
