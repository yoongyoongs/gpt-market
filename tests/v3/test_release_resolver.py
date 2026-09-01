"""RC-07B Runtime Release Resolver 测试（STR-002）。

整改方案 §10.4/§10.5：
- 唯一 Runtime Strategy Resolver：ReleaseState → strategy_version →
  feature/recall/config versions → current executor configuration；
- 禁止"DB 说 V3，进程仍 hardcode 跑 V2"——Runtime 必须消费 resolver；
- Feature flag 是紧急总开关：关闭 → 立即 V2 fallback；
- 回滚必须立即生效：每次 resolve 都重新读最新 ReleaseState，不缓存。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.v3.application.release_resolver import ReleaseResolver

NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


class _FakeStrategyRepo:
    def __init__(self, resolution):
        self._resolution = resolution
        self.calls = []

    async def resolve_release(self, environment):
        self.calls.append(environment)
        return self._resolution()


class _FakeUow:
    def __init__(self, strategies):
        self.strategies = strategies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None


def _v3_resolution(**overrides):
    resolution = {
        "mode": "V3", "effective_mode": "V3", "reason": None,
        "strategy_version_id": uuid4(),
        "guardrail_version_id": uuid4(),
        "configuration": {"feature_version": "feat-v2", "recall": ["momentum"],
                          "horizons": [1, 3, 5, 10, 20]},
        "row_version": 3,
    }
    resolution.update(overrides)
    return resolution


def _service(repo, *, v3_enabled=True):
    return ReleaseResolver(
        lambda: _FakeUow(repo), v3_enabled=v3_enabled, clock=lambda: NOW,
    )


async def test_disabled_flag_is_emergency_v2_fallback_without_db_read() -> None:
    repo = _FakeStrategyRepo(_v3_resolution)
    report = await _service(repo, v3_enabled=False).resolve("production")
    # 紧急总开关：不读库、立即 V2，绝不给出 V3 executor 配置
    assert repo.calls == []
    assert report["effective_mode"] == "V2"
    assert report["reason"] == "V3_DISABLED_FLAG"
    assert report["strategy_version_id"] is None
    assert report["configuration"] is None
    assert report["resolved_at"] == NOW


async def test_resolved_v3_release_exposes_executor_configuration() -> None:
    resolution = _v3_resolution()
    report = await _service(_FakeStrategyRepo(lambda: resolution)).resolve("production")
    assert report["effective_mode"] == "V3"
    assert report["reason"] is None
    assert report["strategy_version_id"] == resolution["strategy_version_id"]
    assert report["guardrail_version_id"] == resolution["guardrail_version_id"]
    assert report["configuration"]["feature_version"] == "feat-v2"
    assert report["row_version"] == 3
    assert report["resolved_at"] == NOW


async def test_no_release_state_falls_back_to_v2() -> None:
    resolution = {"mode": "V2", "effective_mode": "V2", "reason": "NO_V3_RELEASE",
                  "strategy_version_id": None, "guardrail_version_id": None,
                  "configuration": None, "row_version": None}
    report = await _service(_FakeStrategyRepo(lambda: resolution)).resolve("production")
    assert report["effective_mode"] == "V2"
    assert report["reason"] == "NO_V3_RELEASE"


async def test_rollback_takes_effect_immediately_without_restart() -> None:
    state = {"mode": "V3", "row_version": 3, "active": True}

    def resolution():
        if state["active"]:
            return _v3_resolution(row_version=state["row_version"])
        return {"mode": "V2", "effective_mode": "V2", "reason": "RELEASE_MODE_V2",
                "strategy_version_id": None, "guardrail_version_id": None,
                "configuration": None, "row_version": state["row_version"]}

    service = _service(_FakeStrategyRepo(resolution))
    first = await service.resolve("production")
    assert first["effective_mode"] == "V3"
    # 运维执行 rollback（ReleaseState 落库变更）后，同一 resolver 实例
    # 不重启、不缓存 → 下一次 resolve 立即回到 V2
    state["active"] = False
    state["row_version"] = 4
    second = await service.resolve("production")
    assert second["effective_mode"] == "V2"
    assert second["reason"] == "RELEASE_MODE_V2"
    assert second["configuration"] is None
