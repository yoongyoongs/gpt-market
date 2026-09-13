"""Step 20：真实全市场扫描编排测试（离线，fake UoW）。

覆盖：universe 成员与特征行按 code join、未知 security 跳过、
close/bar_count 从特征行注入、pipeline 全链执行、save_scan 落库调用、
缺 universe / 缺 feature_run 早退分支。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
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
    def __init__(self, key_map: dict, regime: dict | None = None) -> None:
        self._key_map = key_map
        self._regime = regime
        self.saved: list = []

    async def security_keys(self) -> dict:
        return self._key_map

    async def regime_snapshot(self, feature_run_id) -> dict | None:
        return self._regime

    async def save_scan(self, result, *, scan_time=None):
        self.saved.append(result)
        return result.scan_id


class _FakeEvidence:
    """PIT 行为与真实 repository 对齐：known_at<=as_of 才返回，
    单次批量接收全部 security_ids（编排器不得逐股循环查询）。"""

    def __init__(self, rows: tuple = ()) -> None:
        self._rows = rows
        self.calls: list[tuple[tuple, datetime]] = []

    async def for_securities(self, security_ids: tuple, *, as_of: datetime):
        self.calls.append((tuple(security_ids), as_of))
        return tuple(
            row for row in self._rows
            if row.security_id in security_ids and row.record.known_at <= as_of
        )


class _FakeUow:
    def __init__(self, universes, features, scans, evidence=None) -> None:
        self.universes = universes
        self.features = features
        self.scans = scans
        self.evidence = evidence or _FakeEvidence()


def _uow(codes=("000001", "000002"), unknown_extra=False, evidence_rows=()):
    members = tuple(_member(code) for code in codes)
    if unknown_extra:
        members = members + (_member("999999"),)  # universe 有但无特征行
    snapshot = _snapshot(members)
    views = tuple(
        _view(uuid4(), code, {**GOOD_FEATURES, "bar_count": 250})
        for code in codes
    )
    key_map = {view.security_id: code for view, code in zip(views, codes)}
    return _FakeUow(
        _FakeUniverses(snapshot), _FakeFeatures(views), _FakeScans(key_map),
        _FakeEvidence(evidence_rows),
    )


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


class TestEvidenceDataChain:
    """第二轮 P0-03：FQ/CAT evidence 数据链经编排器真实接通。"""

    @staticmethod
    def _finance_rows(security_id, *, growth: float = 0.30, known_at=None):
        """跨年两期财报证据（营收/利润同比 ~growth）。"""
        from tests.v3.test_candidate_experts import _evidence

        rows = []
        for year, base in ((2025, 100.0), (2026, 100.0 * (1 + growth))):
            view = _evidence(
                report_name="RPT_F10_FINANCE_MAINFINADATA",
                values={
                    "TOTALOPERATEREVE": f"{base}",
                    "PARENTNETPROFIT": f"{base * 0.2}",
                },
                period=f"{year}-12-31",
            )
            if known_at is not None:
                view = view.model_copy(update={
                    "record": view.record.model_copy(update={"known_at": known_at}),
                })
            rows.append(view.model_copy(update={"security_id": security_id}))
        return tuple(rows)

    @staticmethod
    def _expert_hits(result, expert: str) -> int:
        recall = next(s for s in result.funnel.stages if s.stage == "RECALL")
        return int(recall.extras.get(expert, 0))

    async def test_finance_evidence_produces_fq_hit(self):
        uow = _uow(("000001",))
        target = next(iter(uow.scans._key_map))
        uow.evidence = _FakeEvidence(self._finance_rows(target))
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary["status"] == "ok"
        result = uow.scans.saved[0]
        assert self._expert_hits(result, "FQ") == 1
        # 批量查询：一次调用覆盖全部 security_ids，且 as_of 正确传递
        assert len(uow.evidence.calls) == 1
        called_ids, called_as_of = uow.evidence.calls[0]
        assert set(called_ids) == set(uow.scans._key_map)
        assert called_as_of == NOW

    async def test_catalyst_evidence_produces_cat_hit(self):
        from tests.v3.test_candidate_experts import _evidence

        uow = _uow(("000001",))
        target = next(iter(uow.scans._key_map))
        announcement = _evidence(title="公司拟回购股份暨回购报告书")
        uow.evidence = _FakeEvidence(
            (announcement.model_copy(update={"security_id": target}),),
        )
        await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        result = uow.scans.saved[0]
        assert self._expert_hits(result, "CAT") == 1

    async def test_no_evidence_experts_run_but_fq_cat_silent(self):
        """无 evidence → FQ/CAT 不命中，其它专家照常运行。"""
        uow = _uow(("000001", "000002"))
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary["status"] == "ok"
        result = uow.scans.saved[0]
        assert self._expert_hits(result, "FQ") == 0
        assert self._expert_hits(result, "CAT") == 0
        recall = next(s for s in result.funnel.stages if s.stage == "RECALL")
        assert sum(int(v) for k, v in recall.extras.items() if k not in {"FQ", "CAT"}) > 0

    async def test_future_known_evidence_excluded(self):
        """known_at > as_of 的证据不得进入当期扫描（PIT）。"""
        uow = _uow(("000001",))
        target = next(iter(uow.scans._key_map))
        future_rows = self._finance_rows(target, known_at=NOW + timedelta(days=1))
        uow.evidence = _FakeEvidence(future_rows)
        await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        result = uow.scans.saved[0]
        assert self._expert_hits(result, "FQ") == 0


_REGIME = {
    "regime_snapshot_id": "snap-1",
    "index_states": {"status": "UP"},
    "breadth": {"advance_decline_ratio": 1.0, "mean_return_3d": 0.0, "observed": 100},
    "risk_appetite_facts": {"volume_expansion_count": 10, "breakout_20d_count": 5},
    "coverage": 0.8,
    "stale": False,
}


class TestMarketRegimePit:
    """P1-02：regime 快照按 feature_run_id PIT 绑定；stale 不当事实。"""

    async def test_valid_regime_score_in_summary(self):
        uow = _uow(("000001",))
        uow.scans._regime = dict(_REGIME)
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary["status"] == "ok"
        assert summary["market_regime_score"] == 68.0
        assert summary["market_regime_source"] == "snap-1"
        entry = uow.scans.saved[0].deep.entries[0]
        detail = entry.components_detail["market_regime"]
        assert detail["raw"] == 68.0
        assert detail["source"] == "snap-1"

    async def test_stale_regime_not_a_fact(self):
        uow = _uow(("000001",))
        uow.scans._regime = {**_REGIME, "stale": True}
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary["market_regime_score"] is None
        assert summary["market_regime_source"] is None
        entry = uow.scans.saved[0].deep.entries[0]
        detail = entry.components_detail["market_regime"]
        assert detail["missing"] is True

    async def test_no_regime_snapshot_missing(self):
        uow = _uow(("000001",))
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary["market_regime_score"] is None
