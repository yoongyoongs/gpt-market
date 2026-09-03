"""RT §21 盘中触发循环（部署裁决：现有 worker 容器内轻量循环，不新增容器）。

RT-01~03 的盘中能力此前只有 API/MCP 按需拉取——没人访问就没数据，
AttentionEvent 的盘中评估没有触发点。本循环补上确定性触发：

- 交易时段（XSHG 交易日 + 09:30-11:30 / 13:00-15:00 CST）内每
  V3_INTRADAY_INTERVAL_SECONDS（默认 300，保守频率）评估一轮；
- 每轮：各 decision 最新 plan 的 stop/target × 实时 quote →
  AttentionEngineService.evaluate_entry_plan_levels（去抖由 engine 负责）；
- 行情失败/quote 缺失：跳过该计划并如实计数，绝不伪造价格；
- 不产生 Trade、不改 Decision——只落 AttentionEvent（engine append-only）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time
from typing import Any, Protocol

from app.utils.time import SHANGHAI


# A 股连续竞价时段（不含集合竞价，保守起点）
TRADING_SESSIONS: tuple[tuple[time, time], ...] = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(15, 0)),
)
IDLE_POLL_SECONDS = 60.0


def in_trading_session(
    local_time: time, sessions: tuple[tuple[time, time], ...] = TRADING_SESSIONS,
) -> bool:
    return any(start <= local_time <= end for start, end in sessions)


class _PlansRepo(Protocol):
    async def active_price_trigger_plans(self) -> tuple[dict, ...]: ...


class _Uow(Protocol):
    ai_imports: _PlansRepo

    async def __aenter__(self) -> "_Uow": ...

    async def __aexit__(self, *args) -> None: ...


class _AttentionEngine(Protocol):
    async def evaluate_entry_plan_levels(self, **kwargs) -> Any: ...


class _QuoteService(Protocol):
    async def get_quote_snapshot(self, code: str, *, as_of: datetime) -> Any: ...


class IntradayTriggerLoop:
    def __init__(
        self,
        uow_factory: Callable[[], _Uow],
        quote_service: _QuoteService,
        engine: _AttentionEngine,
        is_trading_day: Callable[..., bool],
        *,
        interval_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._uow_factory = uow_factory
        self._quote_service = quote_service
        self._engine = engine
        self._is_trading_day = is_trading_day
        self._interval = interval_seconds
        self._clock = clock or (
            lambda: datetime.now(datetime.now().astimezone().tzinfo)
        )

    async def evaluate_once(self) -> dict:
        """单轮评估：读最新带 stop/target 的 plan，逐计划拉实时行情触发。"""
        as_of = self._clock()
        async with self._uow_factory() as uow:
            plans = await uow.ai_imports.active_price_trigger_plans()
        summary = {
            "plan_count": len(plans),
            "evaluated": 0,
            "quote_failed": 0,
            "engine_failed": 0,
            "created": 0,
            "skipped": 0,
            "as_of": as_of.isoformat(),
        }
        for item in plans:
            try:
                quote = await self._quote_service.get_quote_snapshot(
                    item["code"], as_of=as_of,
                )
            except Exception:  # noqa: BLE001 - 单计划行情失败不阻断其余
                summary["quote_failed"] += 1
                continue
            plan_levels = {
                "stop_loss": item["stop_loss"],
                "take_profit": item["take_profit"],
            }
            try:
                evaluation = await self._engine.evaluate_entry_plan_levels(
                    entry_plan_id=item["entry_plan_id"],
                    security_id=item["security_id"],
                    code=item["code"],
                    market=item["market"],
                    plan=plan_levels,
                    quote=quote,
                    as_of=as_of,
                )
            except Exception:  # noqa: BLE001 - 单计划引擎失败不阻断其余
                summary["engine_failed"] += 1
                continue
            summary["evaluated"] += 1
            summary["created"] += len(getattr(evaluation, "created", ()) or ())
            summary["skipped"] += getattr(evaluation, "skipped", 0) or 0
        return summary

    async def run_forever(self) -> None:
        """常驻循环：非交易时段低频空转，时段内按间隔评估。"""
        while True:
            local_now = self._clock().astimezone(SHANGHAI)
            try:
                trading_day = bool(self._is_trading_day(local_now.date()))
            except Exception:  # noqa: BLE001 - 日历失败按非交易时段空转
                trading_day = False
            if not trading_day or not in_trading_session(local_now.time()):
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            try:
                await self.evaluate_once()
            except Exception:  # noqa: BLE001 - 单轮失败不终止常驻循环
                pass
            await asyncio.sleep(self._interval)
