from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.providers.eastmoney import EastmoneyProvider

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["002284", "600722", "600519", "000001"])
async def test_live_quote(code: str) -> None:
    provider = EastmoneyProvider(Settings())
    try:
        result = await provider.get_quote(code)
        assert result.code == code
        assert result.price is not None and result.price > 0
        assert result.data_timestamp is not None
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_live_daily_kline() -> None:
    provider = EastmoneyProvider(Settings())
    try:
        result = await provider.get_kline("002284", "day", 5)
        assert len(result.klines) == 5
    finally:
        await provider.close()
