from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any

from app.api.live import market_status
from app.utils.time import now_shanghai


logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class V2DashboardState:
    result: Any | None
    updated_at: datetime | None
    last_error: str | None


class V2DashboardCache:
    """Keep the default V2 dashboard scan off the HTTP request path."""

    def __init__(
        self,
        loader: Callable[[], Awaitable[Any]],
        *,
        trading_interval: float = 30.0,
        closed_interval: float = 300.0,
    ) -> None:
        self.loader = loader
        self.trading_interval = trading_interval
        self.closed_interval = closed_interval
        self._state = V2DashboardState(result=None, updated_at=None, last_error=None)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def get(self) -> V2DashboardState:
        return self._state

    async def refresh_once(self) -> None:
        started = perf_counter()
        logger.info("V2 看板后台刷新开始")
        try:
            result = await self.loader()
            self._state = V2DashboardState(result=result, updated_at=now_shanghai(), last_error=None)
            logger.info(
                "V2 看板后台刷新成功 scan_id=%s 候选数=%d 耗时=%.3fs",
                getattr(result, "scan_id", "unknown"),
                len(getattr(result, "raw_top30", [])),
                perf_counter() - started,
            )
        except Exception as exc:
            previous = self._state
            error = f"{type(exc).__name__}: {exc}"
            self._state = V2DashboardState(
                result=previous.result,
                updated_at=previous.updated_at,
                last_error=error,
            )
            logger.warning("V2 看板后台刷新失败，保留上一份成功快照 error=%s", error)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            await self.refresh_once()
            interval = self.trading_interval if market_status() == "TRADING" else self.closed_interval
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="v2-dashboard-refresh")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
