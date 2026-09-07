"""R5-P1-007/§65 + F6-06/P1-10：Worker Heartbeat 跨进程读取。

Worker 与 API/Dashboard 是不同进程——Worker 内存里的 heartbeat 对外
不可见是 R5 复验实锤的问题。本服务读 operational_health_events 里
该 component 的最近心跳（Worker 经 build_health_sink 节流落库），
按 capability 取最新一条聚合成状态视图：

- degraded / consecutive_errors / last_error 必须直接可见
  （§65 验收：连续 3 次 Fast Lane 失败 → HTTP 状态接口可见）；
- quote_expected/actual/coverage、active_pool_size、candidate_count、
  deep_count、plan_count、provider_health 全透传（§65 必填字段）；
- **freshness**（F6-06/P1-10）：不能只看最后一条 status——交易时段内
  latest observed_at 距今超过阈值（默认 3 × 最长关键 lane interval =
  1800s）→ STALE/DEGRADED；非交易时段用更宽阈值（默认 6h）。
  Worker 死掉 2 小时后旧 HEALTHY 绝不再显示 HEALTHY。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

# F6-06：交易时段 stale 阈值 = 3 × 最长关键 lane interval（evidence
# 600s）= 1800s；非交易时段放宽到 6h。
DEFAULT_STALE_AFTER_TRADING_SECONDS = 1800.0
DEFAULT_STALE_AFTER_NON_TRADING_SECONDS = 21600.0


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class ReadWorkerHeartbeatService:
    def __init__(
        self, uow_factory: Callable[[], Any],
        *,
        clock: Callable[[], datetime] | None = None,
        trading_session: Callable[[], bool] | None = None,
        stale_after_trading_seconds: float = (
            DEFAULT_STALE_AFTER_TRADING_SECONDS
        ),
        stale_after_non_trading_seconds: float = (
            DEFAULT_STALE_AFTER_NON_TRADING_SECONDS
        ),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now().astimezone())
        # trading_session：交易时段谓词（API 侧用 MarketIntradayStatus
        # 接线）；None → 保守按交易时段阈值处理（宁可误报不漏报）。
        self._trading_session = trading_session
        self._stale_trading = stale_after_trading_seconds
        self._stale_non_trading = stale_after_non_trading_seconds

    def _stale_threshold(self) -> float:
        if self._trading_session is not None:
            try:
                if not self._trading_session():
                    return self._stale_non_trading
            except Exception:  # noqa: BLE001 - 时段判断失败按交易时段
                return self._stale_trading
        return self._stale_trading

    async def execute(
        self, component: str = "intraday-worker", limit: int = 20,
    ) -> dict[str, Any]:
        async with self._uow_factory() as uow:
            rows = await uow.strategies.read_health_events(component, limit)
        capabilities: dict[str, dict[str, Any]] = {}
        # rows 按 observed_at desc——首个出现的 capability 即其最新心跳
        latest: dict[str, Any] | None = None
        now = self._clock()
        threshold = self._stale_threshold()
        for row in rows:
            if latest is None:
                latest = self._view(row, now, threshold)
            if row.capability in capabilities:
                continue
            capabilities[row.capability] = self._view(row, now, threshold)
        consecutive = max(
            (view["consecutive_errors"] for view in capabilities.values()),
            default=0,
        )
        degraded = any(view["degraded"] for view in capabilities.values())
        stale = any(view["heartbeat_stale"] for view in capabilities.values())
        last_error = next(
            (view["last_error"] for view in capabilities.values()
             if view["last_error"] is not None),
            None,
        )
        return {
            "component": component,
            "as_of": now.isoformat(),
            "degraded": degraded or stale,
            "heartbeat_stale": stale,
            "heartbeat_age_seconds": (
                min(
                    (view["heartbeat_age_seconds"]
                     for view in capabilities.values()),
                    default=None,
                )
            ),
            "consecutive_errors": consecutive,
            "last_error": last_error,
            "heartbeat_stale_after_seconds": threshold,
            "capabilities": capabilities,
            "latest": latest,
        }

    def _view(
        self, row: Any, now: datetime, threshold: float,
    ) -> dict[str, Any]:
        meta = getattr(row, "metadata_payload", None) or {}
        last_error_type = meta.get("last_error_type")
        observed_at = _aware(row.observed_at)
        now_aware = _aware(now)
        age_seconds = max(0.0, (now_aware - observed_at).total_seconds())
        stale = age_seconds > threshold
        return {
            "capability": row.capability,
            "status": "STALE" if stale else row.status,
            "degraded": row.status != "HEALTHY" or stale,
            "observed_at": row.observed_at.isoformat(),
            "heartbeat_age_seconds": round(age_seconds, 1),
            "heartbeat_stale": stale,
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
            "feature_coverage": meta.get("feature_coverage"),
            "active_pool_size": meta.get("active_pool_size"),
            "candidate_count": meta.get("candidate_count"),
            "deep_count": meta.get("deep_count"),
            "plan_count": meta.get("last_plan_count"),
            "last_fast_lane_status": meta.get("last_fast_lane_status"),
            "provider_health": meta.get("provider_health"),
        }
