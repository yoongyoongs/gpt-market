from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api.v2_dashboard_cache import V2DashboardCache


async def test_v2_dashboard_cache_publishes_successful_snapshot() -> None:
    result = SimpleNamespace(scan_id="scan-1", raw_top30=[1, 2])

    async def loader():
        return result

    cache = V2DashboardCache(loader)
    assert cache.get().result is None

    await cache.refresh_once()

    state = cache.get()
    assert state.result is result
    assert state.updated_at is not None
    assert state.last_error is None


async def test_v2_dashboard_cache_keeps_last_success_after_failure() -> None:
    calls = 0
    result = SimpleNamespace(scan_id="scan-1", raw_top30=[])

    async def loader():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("provider unavailable")
        return result

    cache = V2DashboardCache(loader)
    await cache.refresh_once()
    first_updated_at = cache.get().updated_at
    await cache.refresh_once()

    state = cache.get()
    assert state.result is result
    assert state.updated_at == first_updated_at
    assert state.last_error == "RuntimeError: provider unavailable"


async def test_v2_dashboard_cache_start_does_not_wait_for_loader() -> None:
    release = asyncio.Event()

    async def loader():
        await release.wait()
        return SimpleNamespace(scan_id="scan-1", raw_top30=[])

    cache = V2DashboardCache(loader)
    await cache.start()
    try:
        await asyncio.sleep(0)
        assert cache.get().result is None
    finally:
        release.set()
        await cache.stop()
