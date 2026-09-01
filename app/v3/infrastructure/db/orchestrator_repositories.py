from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.domain.hashing import canonical_hash
from app.v3.infrastructure.db.models import OrchestratorJobRunModel


class SQLAlchemyOrchestratorJobRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_advisory_lock(self, key: str) -> bool:
        result = await self._session.execute(
            text("select pg_try_advisory_lock(hashtext(:key))"), {"key": key},
        )
        return bool(result.scalar_one())

    async def advisory_unlock(self, key: str) -> None:
        await self._session.execute(
            text("select pg_advisory_unlock(hashtext(:key))"), {"key": key},
        )

    async def job_lock(self, job_id: str) -> None:
        await self._session.execute(
            text("select pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"v3-orchestrator:{job_id}"},
        )

    async def latest_succeeded_metrics(
        self, job_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = await self._session.scalar(
            select(OrchestratorJobRunModel)
            .where(
                OrchestratorJobRunModel.job_id == job_id,
                OrchestratorJobRunModel.idempotency_key == idempotency_key,
                OrchestratorJobRunModel.status == "SUCCEEDED",
            )
            .order_by(OrchestratorJobRunModel.attempt.desc())
            .limit(1)
        )
        if row is None:
            return None
        # rollback 会 expire ORM 对象：在会话内复制为普通 dict
        return dict(row.metrics or {})

    async def has_succeeded(self, job_id: str, idempotency_key: str) -> bool:
        existing = await self._session.scalar(
            select(OrchestratorJobRunModel.job_run_id).where(
                OrchestratorJobRunModel.job_id == job_id,
                OrchestratorJobRunModel.idempotency_key == idempotency_key,
                OrchestratorJobRunModel.status == "SUCCEEDED",
            )
        )
        return existing is not None

    async def next_attempt(self, job_id: str, idempotency_key: str) -> int:
        existing = (
            await self._session.scalars(
                select(OrchestratorJobRunModel.attempt).where(
                    OrchestratorJobRunModel.job_id == job_id,
                    OrchestratorJobRunModel.idempotency_key == idempotency_key,
                )
            )
        ).all()
        return max(existing, default=0) + 1

    async def record(
        self,
        *,
        orchestrator_run_id: UUID,
        job_id: str,
        idempotency_key: str,
        attempt: int,
        status: str,
        known_at: datetime,
        as_of: datetime | None,
        started_at: datetime | None,
        completed_at: datetime | None,
        error_type: str | None,
        error_summary: str | None,
        metrics: dict[str, Any],
    ) -> UUID:
        payload = {
            "orchestrator_run_id": str(orchestrator_run_id),
            "job_id": job_id,
            "idempotency_key": idempotency_key,
            "attempt": attempt,
            "status": status,
            "as_of": as_of.isoformat() if as_of else None,
            "known_at": known_at.isoformat(),
            "started_at": started_at.isoformat() if started_at else None,
            "completed_at": completed_at.isoformat() if completed_at else None,
            "error_type": error_type,
            "error_summary": error_summary,
            "metrics": metrics,
        }
        job_run_id = uuid4()
        self._session.add(OrchestratorJobRunModel(
            job_run_id=job_run_id,
            orchestrator_run_id=orchestrator_run_id,
            job_id=job_id, idempotency_key=idempotency_key,
            attempt=attempt, status=status,
            as_of=as_of, known_at=known_at,
            started_at=started_at, completed_at=completed_at,
            error_type=error_type, error_summary=error_summary,
            metrics=metrics, content_hash=canonical_hash(payload),
        ))
        await self._session.flush()
        return job_run_id

