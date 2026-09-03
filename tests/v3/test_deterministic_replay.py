"""RC-06B Deterministic Replay Engine 离线测试。

两层边界（整改方案 §9.2）：
- Server deterministic replay：pinned revisions 上重算 Feature（确定性规则）；
- AI Decision replay：服务器无模型 API，不假装重得同样 Decision；
  有 immutable AI Result 时做"结果回放"，边界必须写进 Replay result。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4


from app.v3.application.calculate_features import CalculateSecurityFeatureService
from app.v3.application.calculate_market_regime import CalculateMarketRegimeService
from app.v3.application.deterministic_replay import (
    _COMPARABLE_FIELDS,
    DeterministicReplayService,
)
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    MarketBar,
    PointInTimePrecision,
)
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import (
    DecayModel,
    EvidenceMatchType,
    EvidenceRepositoryView,
    EvidenceSourceType,
    NormalizedEvidence,
)
from app.v3.domain.performance import ReplayRunCreate

NOW = datetime(2026, 8, 26, 8, tzinfo=timezone.utc)


def _revision(security_id=None, *, known_at=NOW - timedelta(minutes=1)):
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=security_id or uuid4(),
        period=BarPeriod.DAY, adjust_type=AdjustType.QFQ,
        source="fixture", upstream_source="fixture",
        raw_bar_available=False,
        point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="fixture QFQ only",
        known_at=known_at,
        bars=tuple(
            MarketBar(
                bar_time=datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=index),
                open=10.0 + index * 0.1, high=10.2 + index * 0.1,
                low=9.8 + index * 0.1, close=10.0 + index * 0.1,
                volume=1_000_000, amount=1e7,
                fetch_time=NOW - timedelta(minutes=1),
            )
            for index in range(30)
        ),
    ))


def _stored_feature(revision):
    """用同一确定性引擎重算一次，模拟生产落库的特征（可比较列全覆盖）。"""
    feature = CalculateSecurityFeatureService().execute(
        feature_run_id=uuid4(), revision=revision, as_of=NOW,
    )
    payload = feature.model_dump(mode="json")
    stored = {field: payload.get(field) for field in _COMPARABLE_FIELDS}
    stored["daily_trend_state"] = payload["features"]["daily_trend_state"]
    return stored


class _FakePerformanceRepo:
    def __init__(self, checks, stored_features, ai_result, targets=(),
                 regime_inputs=None, pack_payloads=()):
        self._checks = checks
        self._stored = stored_features
        self._ai_result = ai_result
        self._targets = list(targets)
        self._regime_inputs = regime_inputs or {}
        self._pack_payloads = list(pack_payloads)
        self.recorded = []

    async def replay_gate(self, bar_revision_ids, evidence_ids, context_pack_ids, *, replay_as_of):
        revision_set = {"bars": [], "evidence": [], "contexts": []}
        return list(self._checks), revision_set

    async def replay_verification_targets(self, context_pack_ids):
        return [t for t in self._targets if t["context_pack_id"] in set(context_pack_ids)]

    async def load_run_feature(self, feature_run_id, security_id):
        return self._stored.get((feature_run_id, security_id))

    async def regime_replay_input(self, feature_run_id):
        return self._regime_inputs.get(feature_run_id)

    async def context_pack_replay_payloads(self, context_pack_ids):
        wanted = set(context_pack_ids)
        return [item for item in self._pack_payloads if item["context_pack_id"] in wanted]

    async def immutable_ai_result_for_pack(self, context_pack_id):
        return self._ai_result.get(context_pack_id)

    async def record_replay(self, command, payload):
        self.recorded.append(payload)
        return {"replay_run_id": command.replay_run_id, **payload}


class _FakeEvidenceRepository:
    def __init__(self, views):
        self._views = views

    async def retrieve_view(self, *, query):
        from app.v3.domain.evidence import EvidenceRepositoryPage
        return EvidenceRepositoryPage(views=tuple(self._views), coverage_counts={})


class _FakeUow:
    def __init__(self, performance, revisions, evidence_views=()):
        self.performance = performance
        self.bars = self
        self.evidence = _FakeEvidenceRepository(evidence_views)
        self._revisions = revisions

    async def load_revisions_by_ids(self, revision_ids, *, as_of):
        return [r for r in self._revisions if r.revision_id in set(revision_ids)]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None


def _service(performance, revisions, evidence_views=()):
    return DeterministicReplayService(
        lambda: _FakeUow(performance, revisions, evidence_views),
        clock=lambda: NOW,
    )


async def test_gate_blocked_short_circuits_both_layers() -> None:
    performance = _FakePerformanceRepo(
        checks=[{"kind": "bars", "id": str(uuid4()), "passed": False, "reason": "MISSING_INPUT"}],
        stored_features={}, ai_result={},
    )
    command = ReplayRunCreate(
        strategy_version="v1", replay_as_of=NOW, bar_revision_ids=(uuid4(),),
    )
    report = await _service(performance, []).execute(command)
    assert report["status"] == "BLOCKED"
    assert report["result"]["executed"] is False
    layers = report["result"]["layers"]
    assert layers["server_deterministic"]["executed"] is False
    assert layers["ai_decision_replay"]["executed"] is False
    assert performance.recorded and performance.recorded[0]["status"] == "BLOCKED"


async def test_deterministic_layer_recomputes_and_verifies() -> None:
    revision = _revision()
    stored = _stored_feature(revision)
    pack_id = uuid4()
    run_id = uuid4()
    security_id = revision.security_id
    performance = _FakePerformanceRepo(
        checks=[{"kind": "bars", "id": str(revision.revision_id), "passed": True,
                 "reason": None, "known_at": NOW.isoformat(), "replay_as_of": NOW.isoformat()}],
        stored_features={(run_id, security_id): stored},
        ai_result={pack_id: {
            "result_id": uuid4(), "result_type": "DecisionResult",
            "provider": "OPENAI", "model": "gpt", "content_hash": "a" * 64,
            "payload": {"direction": "LONG"},
        }},
        targets=[{"available": True, "context_pack_id": pack_id,
                  "feature_run_id": run_id, "security_id": security_id}],
    )
    command = ReplayRunCreate(
        strategy_version="v1", replay_as_of=NOW,
        bar_revision_ids=(revision.revision_id,),
        context_pack_ids=(pack_id,),
    )
    report = await _service(performance, [revision]).execute(command)
    assert report["status"] == "COMPLETED"
    result = report["result"]
    assert result["executed"] is True
    deterministic = result["layers"]["server_deterministic"]
    assert deterministic["executed"] is True
    assert deterministic["feature_recompute"]["recomputed_count"] == 1
    assert deterministic["feature_recompute"]["verified_count"] == 1
    assert deterministic["feature_recompute"]["matched_count"] == 1
    # 未 pin 的输入（指数/行业 20d 收益）必须显式声明排除，不能静默
    assert "relative_index_strength" in deterministic["excluded_unpinned_inputs"]
    # AI 层边界：服务器无模型 API；有 immutable 结果时是"结果回放"
    ai_layer = result["layers"]["ai_decision_replay"]
    assert ai_layer["executed"] is False
    assert ai_layer["boundary"] == "SERVER_HAS_NO_MODEL_API"
    assert ai_layer["immutable_result_replay"]["available"] is True
    assert ai_layer["immutable_result_replay"]["mode"] == "RESULT_REPLAY_FROM_IMMUTABLE_OUTPUT"


async def test_feature_mismatch_is_recorded_not_swallowed() -> None:
    revision = _revision()
    stored = _stored_feature(revision)
    stored["close"] = stored["close"] + 1.0
    pack_id, run_id = uuid4(), uuid4()
    performance = _FakePerformanceRepo(
        checks=[{"kind": "bars", "id": str(revision.revision_id), "passed": True,
                 "reason": None, "known_at": NOW.isoformat(), "replay_as_of": NOW.isoformat()}],
        stored_features={(run_id, revision.security_id): stored},
        ai_result={},
        targets=[{"available": True, "context_pack_id": pack_id,
                  "feature_run_id": run_id, "security_id": revision.security_id}],
    )
    command = ReplayRunCreate(
        strategy_version="v1", replay_as_of=NOW,
        bar_revision_ids=(revision.revision_id,), context_pack_ids=(pack_id,),
    )
    report = await _service(performance, [revision]).execute(command)
    deterministic = report["result"]["layers"]["server_deterministic"]
    assert deterministic["feature_recompute"]["matched_count"] == 0
    assert deterministic["feature_recompute"]["mismatched"] and (
        deterministic["feature_recompute"]["mismatched"][0]["field"] == "close"
    )


async def test_no_immutable_ai_output_records_honest_boundary() -> None:
    revision = _revision()
    performance = _FakePerformanceRepo(
        checks=[{"kind": "bars", "id": str(revision.revision_id), "passed": True,
                 "reason": None, "known_at": NOW.isoformat(), "replay_as_of": NOW.isoformat()}],
        stored_features={}, ai_result={},
    )
    command = ReplayRunCreate(
        strategy_version="v1", replay_as_of=NOW,
        bar_revision_ids=(revision.revision_id,),
    )
    report = await _service(performance, [revision]).execute(command)
    ai_layer = report["result"]["layers"]["ai_decision_replay"]
    assert ai_layer["executed"] is False
    assert ai_layer["immutable_result_replay"]["available"] is False
    assert ai_layer["immutable_result_replay"]["reason"] == "NO_IMMUTABLE_AI_OUTPUT"


def _regime_fixture():
    """PF-002：用同一确定性引擎模拟"落库 Regime 快照 + 特征行"。"""
    revision = _revision()
    feature = CalculateSecurityFeatureService().execute(
        feature_run_id=uuid4(), revision=revision, as_of=NOW,
    )
    regime = CalculateMarketRegimeService().execute(
        feature_run_id=uuid4(), features=(feature,),
        as_of=NOW, known_at=NOW, expected_count=1, index_benchmark=None,
    )
    dump = regime.model_dump(mode="json")
    stored = {
        "breadth": dump["breadth"], "turnover": dump["turnover"],
        "risk_appetite_facts": dump["risk_appetite_facts"],
        "stale": dump["stale"], "stale_reason": dump["stale_reason"],
    }
    rows = [{
        "stale": feature.stale, "return_3d": feature.return_3d,
        "amount": feature.amount, "volume_expansion": feature.volume_expansion,
        "breakout_20d": feature.breakout_20d,
    }]
    return stored, rows


def _gate_ok():
    return [{"kind": "bars", "id": str(uuid4()), "passed": True,
             "reason": None, "known_at": NOW.isoformat(),
             "replay_as_of": NOW.isoformat()}]


async def test_regime_recompute_verifies_from_stored_rows() -> None:
    """PF-002：Regime 层——从 immutable 落库特征行重算聚合并核验通过；
    未 pin 输入（index_states 等）显式声明排除。"""
    stored, rows = _regime_fixture()
    pack_id, run_id = uuid4(), uuid4()
    performance = _FakePerformanceRepo(
        checks=_gate_ok(), stored_features={}, ai_result={},
        targets=[{"available": True, "context_pack_id": pack_id,
                  "feature_run_id": run_id, "security_id": uuid4()}],
        regime_inputs={run_id: {"regime": stored, "features": rows}},
    )
    command = ReplayRunCreate(
        strategy_version="v1", replay_as_of=NOW,
        bar_revision_ids=(uuid4(),), context_pack_ids=(pack_id,),
    )
    report = await _service(performance, []).execute(command)
    regime_layer = (
        report["result"]["layers"]["server_deterministic"]["regime_recompute"]
    )
    assert regime_layer["executed"] is True
    assert regime_layer["checked_count"] == 1
    assert regime_layer["matched_count"] == 1
    assert regime_layer["mismatched"] == []
    assert "index_states" in regime_layer["excluded_fields"]
    assert regime_layer["exclusion_reason"] == (
        "INDEX_BENCHMARK_AND_EXPECTED_COUNT_INPUTS_NOT_PINNED"
    )


async def test_regime_mismatch_is_recorded_not_swallowed() -> None:
    """PF-002：Regime 重算与落库快照不一致 → 逐字段记录，不吞掉。"""
    stored, rows = _regime_fixture()
    stored["breadth"]["advancing"] += 1  # 注入漂移
    pack_id, run_id = uuid4(), uuid4()
    performance = _FakePerformanceRepo(
        checks=_gate_ok(), stored_features={}, ai_result={},
        targets=[{"available": True, "context_pack_id": pack_id,
                  "feature_run_id": run_id, "security_id": uuid4()}],
        regime_inputs={run_id: {"regime": stored, "features": rows}},
    )
    command = ReplayRunCreate(
        strategy_version="v1", replay_as_of=NOW,
        bar_revision_ids=(uuid4(),), context_pack_ids=(pack_id,),
    )
    report = await _service(performance, []).execute(command)
    regime_layer = (
        report["result"]["layers"]["server_deterministic"]["regime_recompute"]
    )
    assert regime_layer["matched_count"] == 0
    assert "breadth.advancing" in [
        item["field"] for item in regime_layer["mismatched"]
    ]


def _pack_view(index: int, *, contrary: bool = False) -> EvidenceRepositoryView:
    known_at = NOW - timedelta(seconds=index + 1)
    record = NormalizedEvidence.build(
        raw_document_id=uuid4(), evidence_type=EvidenceType.NEWS,
        source_type=EvidenceSourceType.NEWS, source_priority=50,
        subject_type="SECURITY", subject_id="SH:600000",
        claim_key=f"news:{index}", source="fixture", upstream_source="fixture.test",
        payload={"raw": "never-copy"},
        normalized_payload={
            "side": "CONTRARY" if contrary else "SUPPORT",
            "summary": "x" * 200,
        },
        fetch_time=known_at, known_at=known_at, confidence=0.8,
        relevance=0.9 - index / 100, decay_model=DecayModel.NONE,
        parser_version="fixture.v1",
    )
    return EvidenceRepositoryView(
        record=record, match_type=EvidenceMatchType.DIRECT,
        conflict_status="OPEN" if contrary else "NONE",
    )


def _pack_payload(views, candidate_ids):
    return {
        "subject": {"type": "SECURITY"},
        "evidence": {
            "boundary": "UNTRUSTED_DATA",
            "candidate_count": len(candidate_ids),
            "candidate_evidence_ids": candidate_ids,
            "retrieval_config": {"version": "context-evidence-retrieval.v1"},
            "items": [{"evidence_id": value} for value in candidate_ids],
        },
    }


async def test_context_evidence_replay_matches_stored_selection() -> None:
    """PF-002：Context 层——同一排序 + 预算裁剪规则重导出入选证据序列，
    与 immutable payload 核验一致。输入顺序 ≠ 排名顺序。"""
    contrary = _pack_view(0, contrary=True)
    support = _pack_view(1)
    views = [support, contrary]  # 故意逆序放入，验证重放排序
    ranked_ids = [
        str(contrary.record.evidence_id), str(support.record.evidence_id),
    ]
    pack_id = uuid4()
    performance = _FakePerformanceRepo(
        checks=_gate_ok(), stored_features={}, ai_result={},
        pack_payloads=[{
            "context_pack_id": pack_id, "available": True,
            "payload": _pack_payload(views, ranked_ids),
            "as_of": NOW, "subject_type": "SECURITY",
            "subject_id": "SH:600000", "context_level": "NORMAL",
            "token_budget": 6500,
        }],
    )
    command = ReplayRunCreate(
        strategy_version="v1", replay_as_of=NOW,
        bar_revision_ids=(uuid4(),), context_pack_ids=(pack_id,),
    )
    report = await _service(performance, [], evidence_views=views).execute(command)
    layer = (
        report["result"]["layers"]["server_deterministic"]
        ["context_evidence_replay"]
    )
    assert layer["executed"] is True
    assert layer["checked_count"] == 1
    assert layer["matched_count"] == 1
    assert layer["mismatched"] == []


async def test_context_evidence_replay_detects_ranking_drift() -> None:
    """PF-002：候选排序漂移 → 逐字段记录，不吞掉。"""
    contrary = _pack_view(0, contrary=True)
    support = _pack_view(1)
    views = [contrary, support]
    drift_ids = [
        str(support.record.evidence_id), str(contrary.record.evidence_id),
    ]
    pack_id = uuid4()
    performance = _FakePerformanceRepo(
        checks=_gate_ok(), stored_features={}, ai_result={},
        pack_payloads=[{
            "context_pack_id": pack_id, "available": True,
            "payload": _pack_payload(views, drift_ids),
            "as_of": NOW, "subject_type": "SECURITY",
            "subject_id": "SH:600000", "context_level": "NORMAL",
            "token_budget": 6500,
        }],
    )
    command = ReplayRunCreate(
        strategy_version="v1", replay_as_of=NOW,
        bar_revision_ids=(uuid4(),), context_pack_ids=(pack_id,),
    )
    report = await _service(performance, [], evidence_views=views).execute(command)
    layer = (
        report["result"]["layers"]["server_deterministic"]
        ["context_evidence_replay"]
    )
    assert layer["matched_count"] == 0
    assert "candidate_evidence_ids" in [
        item["field"] for item in layer["mismatched"]
    ]
