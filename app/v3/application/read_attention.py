"""RT-08：Attention 事件只读服务（实时方案 §19/§27 RT-08）。

把 RT-04 的 AttentionEvent 仓库能力暴露为聚合 READ：
只读、绝不修改事件状态；带 §18.4 点时字段。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable


class ReadAttentionEventsService:
    def __init__(
        self, uow_factory: Callable[[], Any], *, clock: Callable[[], datetime]
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(
        self,
        *,
        codes: list[str] | None = None,
        entry_plan_id: Any = None,
        event_types: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        now = self._clock()
        async with self._uow_factory() as uow:
            events = await uow.attention.open_events(
                codes=codes,
                entry_plan_id=entry_plan_id,
                event_types=event_types,
                limit=limit,
            )
        serialized = []
        for event in events:
            if hasattr(event, "model_dump"):
                serialized.append(event.model_dump(mode="json"))
            elif isinstance(event, dict):
                serialized.append(event)
            else:
                serialized.append(
                    {
                        c: getattr(event, c)
                        for c in (
                            "event_id",
                            "event_type",
                            "severity",
                            "code",
                            "market",
                            "message",
                            "status",
                            "known_at",
                            "dedupe_key",
                        )
                        if hasattr(event, c)
                    }
                )
        return {
            "source": "attention-read-v1",
            "known_at": now,
            "filters": {
                "codes": codes,
                "entry_plan_id": entry_plan_id,
                "event_types": event_types,
                "limit": limit,
            },
            "count": len(serialized),
            "events": serialized,
        }
