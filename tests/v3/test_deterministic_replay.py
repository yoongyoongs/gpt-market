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
    def __init__(self, checks, stored_features, ai_result, targets=()):
        self._checks = checks
        self._stored = stored_features
        self._ai_result = ai_result
        self._targets = list(targets)
        self.recorded = []

    async def replay_gate(self, bar_revision_ids, evidence_ids, context_pack_ids, *, replay_as_of):
        revision_set = {"bars": [], "evidence": [], "contexts": []}
        return list(self._checks), revision_set

    async def replay_verification_targets(self, context_pack_ids):
        return [t for t in self._targets if t["context_pack_id"] in set(context_pack_ids)]

    async def load_run_feature(self, feature_run_id, security_id):
        return self._stored.get((feature_run_id, security_id))

    async def immutable_ai_result_for_pack(self, context_pack_id):
        return self._ai_result.get(context_pack_id)

    async def record_replay(self, command, payload):
        self.recorded.append(payload)
        return {"replay_run_id": command.replay_run_id, **payload}


class _FakeUow:
    def __init__(self, performance, revisions):
        self.performance = performance
        self.bars = self
        self._revisions = revisions

    async def load_revisions_by_ids(self, revision_ids, *, as_of):
        return [r for r in self._revisions if r.revision_id in set(revision_ids)]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None


def _service(performance, revisions):
    return DeterministicReplayService(lambda: _FakeUow(performance, revisions), clock=lambda: NOW)


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
