"""RT-04：AttentionEvent PostgreSQL Repository。

append-only + 内容寻址去重；冷却查询按 dedupe_key 取最近 known_at。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.domain.attention import (
    AttentionEventType,
    AttentionStatus,
    IntradayAttentionEvent,
)
from app.v3.infrastructure.db.models import AttentionEventModel


class SQLAlchemyAttentionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, event: IntradayAttentionEvent) -> IntradayAttentionEvent:
        """append-only：内容哈希冲突（完全重复提交）静默幂等返回。"""
        stmt = (
            pg_insert(AttentionEventModel)
            .values(
                attention_event_id=event.attention_event_id,
                subject_type=event.subject_type,
                security_id=event.security_id,
                code=event.code,
                market=event.market,
                account_id=event.account_id,
                entry_plan_id=event.entry_plan_id,
                position_review_id=event.position_review_id,
                event_type=str(event.event_type),
                severity=event.severity,
                facts=event.facts,
                as_of=event.as_of,
                known_at=event.known_at,
                source_snapshot_ids=event.source_snapshot_ids,
                status=str(event.status),
                dedupe_key=event.dedupe_key,
                content_hash=event.content_hash,
            )
            .on_conflict_do_nothing(
                index_elements=["content_hash"],
            )
        )
        await self._session.execute(stmt)
        return event

    async def last_known_at(self, dedupe_key: str) -> datetime | None:
        stmt = (
            select(AttentionEventModel.known_at)
            .where(AttentionEventModel.dedupe_key == dedupe_key)
            .order_by(AttentionEventModel.known_at.desc())
            .limit(1)
        )
        return (await self._session.scalars(stmt)).first()

    async def open_events(
        self,
        *,
        codes: list[str] | None = None,
        entry_plan_id: UUID | None = None,
        event_types: list[str] | None = None,
        limit: int = 100,
    ) -> list[IntradayAttentionEvent]:
        stmt = (
            select(AttentionEventModel)
            .where(AttentionEventModel.status == str(AttentionStatus.OPEN))
            .order_by(AttentionEventModel.known_at.desc())
            .limit(limit)
        )
        if codes:
            stmt = stmt.where(AttentionEventModel.code.in_(codes))
        if entry_plan_id is not None:
            stmt = stmt.where(AttentionEventModel.entry_plan_id == entry_plan_id)
        if event_types:
            stmt = stmt.where(AttentionEventModel.event_type.in_(event_types))
        rows = (await self._session.scalars(stmt)).all()
        return [
            IntradayAttentionEvent(
                subject_type=row.subject_type,
                security_id=row.security_id,
                code=row.code,
                market=row.market,
                account_id=row.account_id,
                entry_plan_id=row.entry_plan_id,
                position_review_id=row.position_review_id,
                event_type=AttentionEventType(row.event_type),
                severity=row.severity,
                facts=row.facts,
                as_of=row.as_of,
                known_at=row.known_at,
                source_snapshot_ids=row.source_snapshot_ids,
                status=AttentionStatus(row.status),
                dedupe_key=row.dedupe_key,
                content_hash=row.content_hash,
            )
            for row in rows
        ]

    async def update_status(
        self, attention_event_id: UUID, status: AttentionStatus,
    ) -> bool:
        row = await self._session.get(AttentionEventModel, attention_event_id)
        if row is None:
            return False
        row.status = str(status)
        return True
