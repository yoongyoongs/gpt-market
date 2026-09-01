"""统一 Application Audit Helper（RC-08A / AUD-001）。

整改方案 §11.1：所有关键 WRITE 必须"业务 append/write + AuditEvent +
commit"同事务完成，且不在每个 Service 里复制几十行审计样板：

    recorder = AuditRecorder(uow, clock=...)
    await recorder.record(action=..., object_type=..., object_id=...,
                          before=..., after=..., metadata=...)

审计字段（AuditEvent 合同）：actor（principal）、request_id、action、
object type/id、before/after hash（canonical_hash）、result、time、
metadata。before/after 传任意 canonical 值即可，None → 不记 hash。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.v3.domain.audit import AuditEvent
from app.v3.domain.hashing import canonical_hash


class AuditRecorder:
    def __init__(self, uow, *, clock: Callable[[], datetime] | None = None) -> None:
        self._uow = uow
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def record(
        self,
        *,
        action: str,
        object_type: str,
        object_id: str,
        actor_type: str = "HUMAN",
        actor_id: str | None = None,
        request_id: str | None = None,
        before: Any = None,
        after: Any = None,
        result: str = "SUCCESS",
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        audit_id = uuid4()
        await self._uow.audits.add(AuditEvent(
            audit_id=audit_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            request_id=request_id,
            before_hash=(
                canonical_hash(before) if before is not None else None
            ),
            after_hash=canonical_hash(after) if after is not None else None,
            result=result,
            event_time=self._clock(),
            metadata=dict(metadata or {}),
        ))
        return audit_id
