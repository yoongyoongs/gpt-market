"""Step 20：真实全市场扫描编排测试（离线，fake UoW）。

覆盖：universe 成员与特征行按 code join、未知 security 跳过、
close/bar_count 从特征行注入、pipeline 全链执行、save_scan 落库调用、
缺 universe / 缺 feature_run 早退分支。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from app.v3.application.scan_universe import UniverseScanOrchestrator
from app.v3.domain.market_data import (
    Market,
    SecurityMember,
    UniverseSnapshot,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)
from tests.v3.test_candidate_pipeline_trace import GOOD_FEATURES

NOW = datetime(2026, 9, 7, 8, tzinfo=timezone.utc)


def _member(code: str, **overrides) -> SecurityMember:
    values = dict(
        code=code,
        market=Market.SZ if code.startswith("0") else Market.SH,
        name=f"股票{code}",
    )
    values.update(overrides)
    return SecurityMember(**values)


def _view(security_id, code, features: dict):
    from app.v3.domain.recall import RecallFeatureView

    return RecallFeatureView(
        feature_run_id=uuid4(),
        security_id=security_id,
        as_of=NOW,
        close=10.0,
        coverage=1.0,
        stale=False,
        features=features,
        source_content_hash=hashlib.sha256(code.encode()).hexdigest(),
    )


def _snapshot(members: tuple) -> UniverseSnapshot:
    content = UniverseSnapshotContent(
        snapshot_id=uuid4(),
        source_code="TEST",
        status=UniverseSnapshotStatus.PRIMARY,
        as_of=NOW,
        fetch_time=NOW,
        known_at=NOW,
        coverage=1.0,
        stale=False,
        members=members,
    )
    return UniverseSnapshot.build(content)


class _FakeFeatures:
    def __init__(self, views: tuple) -> None:
        self._views = views
        self._run = type("Run", (), {"feature_run_id": uuid4()})()

    async def latest_run(self):
        return self._run

    async def features_for_run(self, _run_id):
        return self._views


class _FakeUniverses:
    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    async def latest(self):
        return self._snapshot


class _FakeScans:
    def __init__(self, key_map: dict) -> None:
        self._key_map = key_map
        self.saved: list = []

    async def security_keys(self) -> dict:
        return self._key_map

    async def save_scan(self, result, *, scan_time=None):
        self.saved.append(result)
        return result.scan_id


class _FakeUow:
    def __init__(self, universes, features, scans) -> None:
        self.universes = universes
        self.features = features
        self.scans = scans


def _uow(codes=("000001", "000002"), unknown_extra=False):
    members = tuple(_member(code) for code in codes)
    if unknown_extra:
        members = members + (_member("999999"),)  # universe 有但无特征行
    snapshot = _snapshot(members)
    views = tuple(
        _view(uuid4(), code, {**GOOD_FEATURES, "bar_count": 250})
        for code in codes
    )
    key_map = {view.security_id: code for view, code in zip(views, codes)}
    return _FakeUow(_FakeUniverses(snapshot), _FakeFeatures(views), _FakeScans(key_map))


class TestUniverseScanOrchestrator:
    async def test_full_scan_assembles_and_saves(self):
        uow = _uow(("000001", "000002"))
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary["status"] == "ok"
        assert summary["members"] == 2
        assert summary["feature_rows"] == 2
        assert summary["candidates"] == 2
        assert summary["final_count"] == 2
        funnel = summary["funnel"]
        assert funnel["UNIVERSE"] == 2
        assert funnel["FINAL"] == 2
        assert len(uow.scans.saved) == 1

        # close/bar_count 从特征行注入（ST/停牌等标记从 member）
        result = uow.scans.saved[0]
        universe_trace = result.trace.traces[0]
        assert universe_trace.records[0].alive is True

    async def test_skip_member_without_feature_row(self):
        # universe 3 只，特征行只有 2 只 → 999999 不进 candidates
        uow = _uow(("000001", "000002"), unknown_extra=True)
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        # members 3 但 features 只有 2 只对应 → candidates 2
        assert summary["members"] == 3
        assert summary["candidates"] == 2

    async def test_no_universe(self):
        uow = _FakeUow(_FakeUniverses(None), _FakeFeatures(()), _FakeScans({}))
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary == {"status": "no_universe"}

    async def test_no_feature_run(self):
        class _NoRun:
            async def latest_run(self):
                return None

        uow = _FakeUow(
            _FakeUniverses(_snapshot((_member("000001"),))), _NoRun(), _FakeScans({}),
        )
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary == {"status": "no_feature_run"}

    async def test_st_member_flows_into_safety_drop(self):
        """ST 标记来自 universe member（不来自特征行）。"""
        members = (_member("000001"), _member("000002", is_st=True))
        snapshot = _snapshot(members)
        views = tuple(
            _view(uuid4(), code, {**GOOD_FEATURES, "bar_count": 250})
            for code in ("000001", "000002")
        )
        key_map = {v.security_id: c for v, c in zip(views, ("000001", "000002"))}
        uow = _FakeUow(_FakeUniverses(snapshot), _FakeFeatures(views), _FakeScans(key_map))
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary["funnel"]["UNIVERSE"] == 2
        assert summary["funnel"]["SAFETY"] == 1  # ST 被 Safety 淘汰
