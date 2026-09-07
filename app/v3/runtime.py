"""F6-01/§2：唯一 V3 Production Runtime 构建入口。

Worker / MCP / HTTP / CLI / Production Integration Test 必须复用同一
Runtime Factory——数据能力、Provider、feature_limit、deep_limit、
Coverage、Quality、Deep、ActivePool 规则完全一致。唯一允许的差异是
副作用策略：

- Worker：engine=AttentionEngineService（允许写 Attention/Heartbeat）；
- MCP / 只读扫描：engine=None（READ ONLY，绝不写 AttentionEvent）。

禁止再出现 Worker feature_limit=6000 / MCP feature_limit=2000、
Worker Deep=ON / MCP Deep=None 的两套能力。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class V3RuntimeConfig:
    """冻结的 Runtime 能力参数（env 可调，但 Worker/MCP/HTTP 同源）。"""

    feature_limit: int = 6000
    deep_limit: int = 10

    @classmethod
    def from_env(cls) -> "V3RuntimeConfig":
        return cls(
            feature_limit=_env_int("V3_FASTLANE_FEATURE_LIMIT", 6000),
            deep_limit=_env_int("V3_FASTLANE_DEEP_LIMIT", 10),
        )


class V3Runtime:
    """统一 Runtime：同一 Provider/UoW/Overlay/Scanner/Pool/Deep/Levels。

    fast_lane 的 engine 差异是唯一合法的副作用策略差异。
    """

    def __init__(
        self,
        *,
        uow_factory: Any,
        provider_manager: Any,
        fast_lane: Any,
        deep_service: Any,
        intraday_market_data: Any,
        config: V3RuntimeConfig,
    ) -> None:
        self.uow_factory = uow_factory
        self.provider_manager = provider_manager
        self.fast_lane = fast_lane
        self.deep_service = deep_service
        self.intraday_market_data = intraday_market_data
        self.config = config


def build_v3_runtime(
    uow_factory: Any,
    provider_manager: Any,
    *,
    engine: Any = None,
    clock: Any = None,
    config: V3RuntimeConfig | None = None,
) -> V3Runtime:
    """构建统一 Runtime。

    - engine=None → 只读 Runtime（MCP/HTTP scan；绝不写 Attention）；
    - engine=AttentionEngineService → Worker Runtime（写 Attention）。
    其余能力（feature_limit/deep_limit/Deep/Levels/Coverage/Quality）
    两条路径完全一致。
    """
    from app.v3.application.deep_market_data import DeepMarketDataService
    from app.v3.application.intraday_fast_lane import IntradayFastLaneService
    from app.v3.application.intraday_market_data import (
        IntradayMarketDataService,
    )
    from app.v3.application.intraday_overlay import (
        ActiveIntradayUniverseService,
        IntradayOverlayService,
        IntradayScannerService,
    )

    config = config or V3RuntimeConfig.from_env()
    clock = clock or (lambda: datetime.now(timezone.utc))
    deep_service = DeepMarketDataService(
        provider_manager, source="legacy-provider",
    )
    intraday_market_data = IntradayMarketDataService(provider_manager)

    async def _levels_loader(as_of: datetime) -> dict[str, dict[str, float]]:
        """F6-08：真实 EOD Levels（最新 QFQ DAY revision 20 日窗口）。"""
        async with uow_factory() as uow:
            return await uow.features.daily_levels(as_of=as_of)

    fast_lane = IntradayFastLaneService(
        uow_factory,
        provider_manager,
        IntradayOverlayService(),
        IntradayScannerService(),
        ActiveIntradayUniverseService(),
        engine=engine,
        deep_service=deep_service,
        levels_loader=_levels_loader,
        feature_limit=config.feature_limit,
        deep_limit=config.deep_limit,
        clock=clock,
    )
    return V3Runtime(
        uow_factory=uow_factory,
        provider_manager=provider_manager,
        fast_lane=fast_lane,
        deep_service=deep_service,
        intraday_market_data=intraday_market_data,
        config=config,
    )
