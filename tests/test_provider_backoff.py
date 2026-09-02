"""东财限流韧性（RT-10 部署后发现的问题收口）。

生产现象：push2his/push2 对云服务器 IP 间歇性连接层丢弃（TLS 成功后
请求被静默丢掉），原有 0.2s/0.4s 退避毫无意义。收口：

- _request 使用可配置的指数退避 + 抖动（默认 2s 起、指数增长、上限封顶）；
- 指数 K 线（基准摄取）单独使用更高的尝试次数与更长退避——它跑在
  收盘后 Job 里，多花几十秒换成功率是正确取舍。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from app.config import Settings
from app.providers.eastmoney import EastmoneyProvider


def _settings(**overrides) -> Settings:
    values = {
        "eastmoney_timeout": 1.0,
        "eastmoney_retries": 3,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


class _StubTransport(httpx.AsyncBaseTransport):
    """永远连接层失败，触发退避路径。"""

    def handle_async_request(self, request):
        raise httpx.ConnectError("connection dropped", request=request)


def _provider(settings) -> EastmoneyProvider:
    provider = EastmoneyProvider(settings)
    provider._client = httpx.AsyncClient(transport=_StubTransport())
    return provider


@pytest.mark.asyncio
async def test_request_uses_configured_exponential_backoff_with_jitter() -> None:
    settings = _settings(eastmoney_backoff_base=2.0, eastmoney_backoff_cap=60.0)
    provider = _provider(settings)
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch.object(asyncio, "sleep", _fake_sleep), pytest.raises(Exception):
        await provider._request(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {},
        )
    # 3 次尝试之间有 2 次退避；指数增长：base*1, base*2（带抖动）
    assert len(sleeps) == 2
    assert 2.0 <= sleeps[0] <= 2.0 * 1.5
    assert 4.0 <= sleeps[1] <= 4.0 * 1.5


@pytest.mark.asyncio
async def test_request_backoff_is_capped() -> None:
    settings = _settings(eastmoney_backoff_base=2.0, eastmoney_backoff_cap=3.0)
    provider = _provider(settings)
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch.object(asyncio, "sleep", _fake_sleep), pytest.raises(Exception):
        await provider._request(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {},
            attempts=5,
        )
    # 上限封顶：base*2^3=16 → cap=3
    assert all(value <= 3.0 for value in sleeps)


def test_backoff_settings_have_sane_defaults() -> None:
    settings = _settings()
    assert settings.eastmoney_backoff_base == 2.0
    assert settings.eastmoney_backoff_cap == 60.0


@pytest.mark.asyncio
async def test_index_kline_uses_hardened_attempts() -> None:
    """指数基准摄取跑在收盘后 Job：5 次尝试换成功率（股票行情保持快速失败）。"""
    settings = _settings(eastmoney_retries=1)
    provider = _provider(settings)
    recorded: list[int] = []

    async def _fake_request(url, params, *, require_key=None, attempts=None):
        recorded.append(attempts)
        raise RuntimeError("stop")

    with patch.object(provider, "_request", _fake_request), pytest.raises(RuntimeError):
        await provider.get_index_kline("000300", "SH")
    assert recorded == [5]
