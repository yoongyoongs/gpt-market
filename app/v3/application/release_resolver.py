"""Runtime Release Resolver（RC-07B / STR-002）。

整改方案 §10.4/§10.5：消除"数据库 ReleaseState 说 V3、进程仍 hardcode 跑
V2"的断裂——Runtime 必须通过唯一解析点获取当前执行配置：

    ReleaseState → strategy_version → feature/recall/config versions
    → current executor configuration

- Feature flag（V3_ENABLED）是紧急总开关：关闭 → 立即 V2 fallback，
  且不读数据库（V3 链路整体旁路）；
- 回滚立即生效：resolve() 每次都重新读取最新 ReleaseState，不做任何
  缓存；运维 rollback 落库后，下一次 resolve 即回到 V2，新任务按解析
  结果执行，历史 V3 记录 immutable 保留；
- V3 状态但缺少 active 策略版本等不完整状态 → 显式 reason 回落 V2，
  绝不伪造 executor 配置。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


class ReleaseResolver:
    def __init__(
        self,
        uow_factory: Callable,
        *,
        v3_enabled: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._v3_enabled = v3_enabled
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def resolve(self, environment: str = "production") -> dict[str, Any]:
        resolved_at = self._clock()
        if not self._v3_enabled:
            # 紧急总开关：不读库，立即 V2 fallback
            return {
                "environment": environment, "resolved_at": resolved_at,
                "mode": None, "effective_mode": "V2",
                "reason": "V3_DISABLED_FLAG", "strategy_version_id": None,
                "guardrail_version_id": None, "configuration": None,
                "row_version": None,
            }
        async with self._uow_factory() as uow:
            state = await uow.strategies.resolve_release(environment)
        return {"environment": environment, "resolved_at": resolved_at, **state}
