"""R5-P1-007/§65：Worker Heartbeat 跨进程读取。

Worker 与 API/Dashboard 是不同进程——Worker 内存里的 heartbeat 对外
不可见是 R5 复验实锤的问题。本服务读 operational_health_events 里
该 component 的最近心跳（Worker 经 build_health_sink 节流落库），
按 capability 取最新一条聚合成状态视图：

- degraded / consecutive_errors / last_error 必须直接可见
  （§65 验收：连续 3 次 Fast Lane 失败 → HTTP 状态接口可见）；
- quote_expected/actual/coverage、active_pool_size、candidate_count、
  deep_count、plan_count、provider_health 全透传（§65 必填字段）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any


class ReadWorkerHeartbeatService:
    def __init__(
        self, uow_factory: Callable[[], Any],
        *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now().astimezone())

    async def execute(
        self, component: str = "intraday-worker", limit: int = 20,
    ) -> dict[str, Any]:
        async with self._uow_factory() as uow:
            rows = await uow.strategies.read_health_events(component, limit)
        capabilities: dict[str, dict[str, Any]] = {}
        # rows 按 observed_at desc——首个出现的 capability 即其最新心跳
        latest: dict[str, Any] | None = None
        for row in rows:
            if latest is None:
                latest = self._view(row)
            if row.capability in capabilities:
                continue
            capabilities[row.capability] = self._view(row)
        consecutive = max(
            (view["consecutive_errors"] for view in capabilities.values()),
            default=0,
        )
        degraded = any(view["degraded"] for view in capabilities.values())
        last_error = next(
            (view["last_error"] for view in capabilities.values()
             if view["last_error"] is not None),
            None,
        )
        return {
            "component": component,
            "as_of": self._clock().isoformat(),
            "degraded": degraded,
            "consecutive_errors": consecutive,
            "last_error": last_error,
            "capabilities": capabilities,
            "latest": latest,
        }

    @staticmethod
    def _view(row: Any) -> dict[str, Any]:
        meta = getattr(row, "metadata_payload", None) or {}
        last_error_type = meta.get("last_error_type")
        return {
            "capability": row.capability,
            "status": row.status,
            "degraded": row.status != "HEALTHY",
            "observed_at": row.observed_at.isoformat(),
            "last_success_at": meta.get("last_success_at"),
            "last_error_at": meta.get("last_error_at"),
            "last_error_type": last_error_type,
            "last_error": (
                meta.get("last_fast_lane_error") or last_error_type
            ),
            "consecutive_errors": int(meta.get("consecutive_errors") or 0),
            "quote_expected": meta.get("quote_expected"),
            "quote_actual": meta.get("quote_actual"),
            "quote_coverage": meta.get("quote_coverage"),
            "active_pool_size": meta.get("active_pool_size"),
            "candidate_count": meta.get("candidate_count"),
            "deep_count": meta.get("deep_count"),
            "plan_count": meta.get("last_plan_count"),
            "last_fast_lane_status": meta.get("last_fast_lane_status"),
            "provider_health": meta.get("provider_health"),
        }
