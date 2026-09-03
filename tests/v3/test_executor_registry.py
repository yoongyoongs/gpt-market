"""STR-001 Executor Registry 离线测试。

仓库级注册表：Strategy Version → 真正执行器（确定性机器层 = 标准
Recall 通道 × 该 subject 最新 PUBLISHED 特征视图）。边界：
- configuration 可选 "recall_channel_codes" 通道子集；未知通道码 →
  该版本不注册（issues 如实记录），绝不上电一个会静默降级的执行器；
- executor 输出不含 wall-clock：同一 (subject, as_of, 数据) 两次执行
  输出一致 → ShadowObservation 的 hash/diff 语义成立；
- 未知 subject / 无特征视图 / 通道不可用 → 异常或如实字段，不伪造。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.v3.application.executor_registry import (
    SubjectKeyError,
    build_executor_registry,
    parse_subject_key,
    resolve_channel_selection,
)
from app.v3.domain.features import PublishedSecurityFeatureView

NOW = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)


def _feature_view(**overrides) -> PublishedSecurityFeatureView:
    values = dict(
        feature_run_id=uuid4(), security_id=uuid4(), series_revision_id=uuid4(),
        as_of=NOW, close=10.0, return_3d=0.03, return_5d=0.04, return_20d=0.1,
        position_60d=0.2, ma20_slope=0.002, breakout_20d=True, pullback_20d=False,
        volume_ratio_5d=1.5, volume_expansion=True,
        relative_index_strength=0.04, relative_industry_strength=0.05,
        coverage=0.9, stale=False,
        features={"daily_trend_state": "UP", "weekly_trend_state": "BASE"},
        input_hash="a" * 64, source_content_hash="b" * 64,
    )
    values.update(overrides)
    return PublishedSecurityFeatureView(**values)


class _FakeUniverseRepo:
    def __init__(self, known: dict[str, object]):
        self._known = known

    async def security_id_by_key(self, market, code):
        return self._known.get(f"{market}:{code}")


class _FakeFeatureRepo:
    def __init__(self, views: dict[object, PublishedSecurityFeatureView]):
        self._views = views

    async def latest_security_feature(self, security_id, *, as_of):
        return self._views.get(security_id)


class _FakeStrategyRepo:
    def __init__(self, versions):
        self._versions = versions

    async def strategy_catalog(self, limit):
        return {
            "strategy_versions": tuple(self._versions),
            "proposals": (), "guardrail_versions": (),
        }


class _FakeUow:
    def __init__(self, universes, features, strategies):
        self.universes = universes
        self.features = features
        self.strategies = strategies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


def _registry_setup(versions, known_subjects, views):
    uow = _FakeUow(
        _FakeUniverseRepo(known_subjects),
        _FakeFeatureRepo(views),
        _FakeStrategyRepo(versions),
    )
    return uow


def test_parse_subject_key_enforces_market_code_contract() -> None:
    assert parse_subject_key("SH:600000") == ("SH", "600000")
    assert parse_subject_key("SZ:000001") == ("SZ", "000001")
    for bad in ("600000.SH", "SH600000", "XX:600000", "SH:", ":600000"):
        with pytest.raises(SubjectKeyError):
            parse_subject_key(bad)


def test_resolve_channel_selection_defaults_to_all_standard_channels() -> None:
    standard = resolve_channel_selection(None)
    standard_empty = resolve_channel_selection({})
    assert standard == standard_empty and len(standard) == 9
    assert "LOW_POSITION_TURNING" in standard
    subset = resolve_channel_selection({
        "recall_channel_codes": ["FIRST_BREAKOUT", "VOLUME_EXPANSION"],
    })
    assert subset == ("FIRST_BREAKOUT", "VOLUME_EXPANSION")
    with pytest.raises(ValueError, match="non-empty"):
        resolve_channel_selection({"recall_channel_codes": []})
    with pytest.raises(ValueError, match="unknown recall channel codes"):
        resolve_channel_selection({"recall_channel_codes": ["NOT_A_CHANNEL"]})


async def test_registry_binds_executors_and_evaluates_channels() -> None:
    view = _feature_view()
    versions = [
        {"strategy_version_id": uuid4(), "configuration": None},
        {
            "strategy_version_id": uuid4(),
            "configuration": {"recall_channel_codes": ["LOW_POSITION_TURNING"]},
        },
        {
            "strategy_version_id": uuid4(),
            "configuration": {"recall_channel_codes": ["NOT_A_CHANNEL"]},
        },
    ]
    uow = _registry_setup(
        versions,
        {"SH:600000": view.security_id},
        {view.security_id: view},
    )
    registry = await build_executor_registry(lambda: uow)
    assert registry["registered_count"] == 2
    assert [
        issue["strategy_version_id"] for issue in registry["issues"]
    ] == [str(versions[2]["strategy_version_id"])]

    default_executor = registry["executors"][versions[0]["strategy_version_id"]]
    output = await default_executor("SH:600000", NOW)
    assert output["subject_key"] == "SH:600000"
    assert output["as_of"] == NOW.isoformat()
    assert output["evaluated_channel_count"] == 9
    assert output["channels"]["LOW_POSITION_TURNING"]["hit"] is True
    assert output["channels"]["FIRST_BREAKOUT"]["hit"] is True
    assert output["channels"]["VOLUME_EXPANSION"]["hit"] is True
    assert output["channels"]["FIRST_PULLBACK"]["hit"] is False  # pullback_20d=False
    assert output["hit_channel_count"] == sum(
        1 for item in output["channels"].values() if item["hit"]
    )
    # 决定性：同输入两次执行逐字段一致（无 wall-clock 泄入输出）
    again = await default_executor("SH:600000", NOW)
    assert again == output

    subset_executor = registry["executors"][versions[1]["strategy_version_id"]]
    subset_output = await subset_executor("SH:600000", NOW)
    assert list(subset_output["channels"]) == ["LOW_POSITION_TURNING"]
    assert subset_output["evaluated_channel_count"] == 1


async def test_executor_honest_failures_not_fabricated() -> None:
    view = _feature_view()
    version_id = uuid4()
    uow = _registry_setup(
        [{"strategy_version_id": version_id, "configuration": None}],
        {"SH:600000": view.security_id},
        {view.security_id: view},
    )
    registry = await build_executor_registry(lambda: uow)
    executor = registry["executors"][version_id]

    # 未知 subject → 异常（Shadow 侧如实记 error，不伪造输出）
    with pytest.raises(LookupError, match="unknown security subject"):
        await executor("SH:688888", NOW)
    # 非契约 subject_key → 异常
    with pytest.raises(SubjectKeyError):
        await executor("600000.SH", NOW)

    # stale 特征视图 → 每个通道如实 unavailable，不编造命中
    stale_view = _feature_view(stale=True, security_id=view.security_id)
    uow_stale = _registry_setup(
        [{"strategy_version_id": version_id, "configuration": None}],
        {"SH:600000": view.security_id},
        {view.security_id: stale_view},
    )
    stale_registry = await build_executor_registry(lambda: uow_stale)
    stale_output = await stale_registry["executors"][version_id]("SH:600000", NOW)
    assert stale_output["hit_channel_count"] == 0
    assert all(
        item["hit"] is False and item["unavailable_reason"]
        for item in stale_output["channels"].values()
    )
