"""RT-06：Entry Decision Context + Typed EntryPlanPayload（实时方案 §8.4/§17.2/§27 RT-06）。

- EntryPlanPayload 类型化：不同 AI Result 绝不各写各的字段；
- Trigger / Cancel 客观评估：只做确定性判断（价格条件），TEXT 条件不可客观评估；
- 验收：能回答"现在能买吗"；缺关键实时数据（quote 缺失/stale/无价格）绝不 READY。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.v3.domain.action import EntryReadiness
from app.v3.domain.entry_plan import EntryPlanPayload
from app.v3.application.read_entry_decision_context import (
    ReadEntryDecisionContextService,
)

NOW = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)

_PLAN = {
    "entry_mode": "PULLBACK_ENTRY",
    "entry_zone": {"low": 9.28, "high": 9.36},
    "triggers": [
        {"kind": "PRICE_ABOVE", "value": 9.30},
        {"kind": "TEXT", "description": "60m 支撑企稳"},
    ],
    "confirms": ["15m 量价确认"],
    "cancels": [{"kind": "PRICE_BELOW", "value": 9.02}],
    "stop": {"price": 9.02, "reason": "跌破前低失效"},
    "targets": [{"price": 9.85, "target_type": "T1"}],
    "max_wait_sessions": 3,
}


def _payload() -> EntryPlanPayload:
    return EntryPlanPayload.from_plan(_PLAN)


# ---------- typed payload ----------


def test_entry_plan_payload_parses_typed_fields() -> None:
    payload = _payload()
    assert payload.entry_mode == "PULLBACK_ENTRY"
    assert payload.entry_zone.low == 9.28
    assert payload.stop.price == 9.02
    assert payload.targets[0].target_type == "T1"
    assert payload.triggers[0].kind == "PRICE_ABOVE"
    assert payload.triggers[1].kind == "TEXT"
    assert payload.max_wait_sessions == 3


def test_price_condition_without_value_rejected() -> None:
    with pytest.raises(ValidationError):
        EntryPlanPayload.from_plan({
            "entry_mode": "BREAKOUT",
            "triggers": [{"kind": "PRICE_ABOVE"}],
        })


# ---------- readiness 评估（通过服务） ----------


class _Quote:
    def __init__(self, last_price=None, stale=False):
        self.last_price = last_price
        self.stale = stale
        self.model_fields_data = {"last_price": last_price, "stale": stale}

    def model_dump(self):
        return dict(self.model_fields_data)


class _QuoteService:
    def __init__(self, quote):
        self._quote = quote

    async def get_quote_snapshot(self, code, *, as_of):
        return self._quote


class _StructureService:
    async def get_snapshot(self, code, *, as_of):
        return None


class _FakeReads:
    def __init__(self, security_id):
        self._security_id = security_id

    async def security_id_by_code(self, market, code):
        return self._security_id


class _FakeDecisionRepo:
    def __init__(self, bundle):
        self._bundle = bundle

    async def read_decision_state(self, security_id):
        return self._bundle


class _FakeUow:
    def __init__(self, reads, decisions):
        self.reads = reads
        self.ai_imports = decisions

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _bundle(plan=_PLAN, entry_plan_id=None, decision_id=None):
    decision_id = decision_id or uuid4()
    entry_plan_id = entry_plan_id or uuid4()
    return {
        "decisions": ({"decision_id": decision_id, "as_of": NOW},),
        "entry_plan_versions": ({
            "entry_plan_id": entry_plan_id,
            "decision_id": decision_id,
            "version": 1,
            "effective_from": NOW,
            "expected_horizon": "D3_10",
            "plan": plan,
        },),
        "reviews": (),
    }


def _service(quote, bundle, security_id=uuid4()):
    def uow_factory():
        return _FakeUow(_FakeReads(security_id), _FakeDecisionRepo(bundle))
    return ReadEntryDecisionContextService(
        uow_factory,
        _QuoteService(quote),
        _StructureService(),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_ready_when_objective_triggers_met_and_cancel_not() -> None:
    report = await _service(_Quote(9.5), _bundle()).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["readiness"] == EntryReadiness.READY
    assert report["readiness_reason"] == "OBJECTIVE_TRIGGERS_MET"
    kinds = {item["kind"] for item in report["evaluated_triggers"]}
    assert kinds == {"PRICE_ABOVE"}  # TEXT 条件不可客观评估，不进入判定


@pytest.mark.asyncio
async def test_wait_trigger_when_trigger_not_met() -> None:
    report = await _service(_Quote(9.1), _bundle()).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["readiness"] == EntryReadiness.WAIT_TRIGGER


@pytest.mark.asyncio
async def test_cancelled_when_cancel_condition_objectively_met() -> None:
    report = await _service(_Quote(8.9), _bundle()).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["readiness"] == EntryReadiness.CANCELLED


@pytest.mark.asyncio
async def test_stale_quote_never_ready() -> None:
    """核心验收：缺关键实时数据不得 READY。"""
    report = await _service(_Quote(9.5, stale=True), _bundle()).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["readiness"] == EntryReadiness.NOT_READY
    assert report["readiness_reason"] == "MISSING_REALTIME_DATA"


@pytest.mark.asyncio
async def test_missing_price_never_ready() -> None:
    report = await _service(_Quote(None), _bundle()).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["readiness"] == EntryReadiness.NOT_READY


@pytest.mark.asyncio
async def test_no_entry_plan_is_not_ready() -> None:
    bundle = _bundle()
    bundle = {**bundle, "entry_plan_versions": ()}
    report = await _service(_Quote(9.5), bundle).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["readiness"] == EntryReadiness.NOT_READY
    assert report["readiness_reason"] == "NO_ENTRY_PLAN"


@pytest.mark.asyncio
async def test_unparseable_plan_is_not_ready_with_reason() -> None:
    bundle = _bundle(plan={"entry_mode": "WEIRD", "triggers": "not-a-list"})
    report = await _service(_Quote(9.5), bundle).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["readiness"] == EntryReadiness.NOT_READY
    assert report["readiness_reason"] == "PLAN_PARSE_FAILED"
    assert report["plan_parse_error"]


@pytest.mark.asyncio
async def test_response_carries_point_in_time_fields() -> None:
    """§18.4：所有实时 READ 必须返回 as_of/known_at/source/stale/quality。"""
    report = await _service(_Quote(9.5), _bundle()).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    for field in ("as_of", "known_at", "source", "stale", "quality", "coverage"):
        assert field in report
    assert report["code"] == "000001"
    assert report["entry_plan"]["entry_plan_id"]
    assert report["plan_payload"]["entry_mode"] == "PULLBACK_ENTRY"


# ---------- PostgreSQL 集成 ----------

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pg_mark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)


@pg_mark
@pytest.mark.asyncio
async def test_security_id_by_code_roundtrip() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.v3.infrastructure.db.models import SecurityModel
    from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    code = "T" + uuid4().hex[:5]
    security_id = uuid4()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        uow._session.add(SecurityModel(
            security_id=security_id, code=code, market="SZ",
            name="RT06测试", security_type="A_SHARE",
        ))
        await uow.commit()
        found = await uow.reads.security_id_by_code("SZ", code)
        missing = await uow.reads.security_id_by_code("SH", code)
    await engine.dispose()
    assert found == security_id
    assert missing is None

# ---------- R4-P1-004 聚合完整事实包 ----------


class _FakeFeatures:
    def __init__(self, regime=None, feature=None):
        self._regime = regime
        self._feature = feature

    async def latest_regime(self):
        return self._regime

    async def latest_security_feature(self, security_id, *, as_of):
        return self._feature


class _FakeRecalls:
    def __init__(self, results=None, raw=None):
        self._results = results
        self._raw = raw

    async def read_results(self, **kwargs):
        return self._results

    async def read_raw(self, **kwargs):
        return self._raw


class _FakeActions:
    def __init__(self, pipeline=()):
        self._pipeline = tuple(pipeline)

    async def read_pipeline(self, security_id, limit=50):
        return self._pipeline


class _FakeAttention:
    def __init__(self, events=()):
        self._events = tuple(events)

    async def open_events(self, *, codes=None, limit=100):
        return list(self._events)


class _FakeDqReads:
    def __init__(self, security_id):
        self._security_id = security_id

    async def security_id_by_code(self, market, code):
        return self._security_id

    async def data_quality(self):
        return {"latest_feature_run": None, "latest_revision_known_at": None}


class _FactUow:
    def __init__(self, reads, features=None, recalls=None, actions=None,
                 attention=None, decisions=None):
        self.reads = reads
        self.features = features or _FakeFeatures()
        self.recalls = recalls or _FakeRecalls()
        self.actions = actions or _FakeActions()
        self.attention = attention or _FakeAttention()
        self.ai_imports = decisions or _FakeDecisionRepo(_bundle())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _View:
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self, mode="json"):
        return dict(self._payload)


class _Event:
    def __init__(self, code):
        self._code = code

    def model_dump(self, mode="json"):
        return {"code": self._code, "event_type": "STOP_HIT"}


def _fact_service(security_id, uow):
    def uow_factory():
        return uow
    return ReadEntryDecisionContextService(
        uow_factory, _QuoteService(_Quote(9.5)), _StructureService(),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_aggregate_facts_populated() -> None:
    """§16：一次响应带全 market_regime/feature_eod/recall/raw/action/
    assessment/attention/data_quality；fundamental/evidence 显式
    NOT_AVAILABLE + reason。"""
    security_id = uuid4()
    uow = _FactUow(
        _FakeDqReads(security_id),
        features=_FakeFeatures(
            regime={"as_of": NOW.isoformat(), "regime": "PULLBACK"},
            feature=_View({"ma20": 9.0}),
        ),
        recalls=_FakeRecalls(
            results=SimpleNamespace(items=(
                SimpleNamespace(market="SH", code="600000"),
                SimpleNamespace(market="SZ", code="000001"),
            )),
            raw=SimpleNamespace(items=(
                SimpleNamespace(market="SZ", code="000001"),
            )),
        ),
        actions=_FakeActions([{
            "raw_opportunity_id": uuid4(),
            "action": {"action_id": uuid4(), "as_of": NOW},
            "entries": ({"entry_assessment_id": uuid4()},),
        }]),
        attention=_FakeAttention([_Event("000001")]),
    )
    report = await _fact_service(security_id, uow).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["market_regime"]["regime"] == "PULLBACK"
    assert report["feature_eod"]["ma20"] == 9.0
    assert report["latest_recall"].code == "000001"
    assert report["latest_raw_opportunity"].code == "000001"
    assert report["latest_action"]["status"] == "AVAILABLE"
    assert report["latest_entry_assessment"]["entry_assessment_id"]
    assert report["attention_events"][0]["code"] == "000001"
    assert "latest_feature_run" in report["data_quality"]
    assert report["fundamental"] == {
        "status": "NOT_AVAILABLE", "reason": "NO_FUNDAMENTAL_SOURCE",
    }
    assert report["evidence"]["reason"] == "NO_EVIDENCE_READ_API"
    assert report["security"]["security_id"] == str(security_id)


@pytest.mark.asyncio
async def test_aggregate_facts_security_unknown() -> None:
    """security 查不到 → 缺口字段统一 SECURITY_UNKNOWN，绝不静默。"""
    uow = _FactUow(_FakeDqReads(None))
    report = await _fact_service(uuid4(), uow).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    for field in ("market_regime", "feature_eod", "latest_recall",
                  "latest_action", "attention_events", "data_quality"):
        assert report[field] == {
            "status": "NOT_AVAILABLE", "reason": "SECURITY_UNKNOWN",
        }
    assert report["security"]["security_id"] is None


@pytest.mark.asyncio
async def test_aggregate_facts_partial_failure_isolated() -> None:
    """单 repo 抛错 → 该字段 UNKNOWN + reason，其余事实照常补齐。"""
    security_id = uuid4()

    class _BoomRecalls:
        async def read_results(self, **kwargs):
            raise RuntimeError("recall db down")

        async def read_raw(self, **kwargs):
            raise RuntimeError("recall db down")

    uow = _FactUow(
        _FakeDqReads(security_id),
        features=_FakeFeatures(regime={"regime": "RANGE"}),
        recalls=_BoomRecalls(),
    )
    report = await _fact_service(security_id, uow).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["latest_recall"]["status"] == "UNKNOWN"
    assert "RuntimeError" in report["latest_recall"]["reason"]
    assert report["market_regime"]["regime"] == "RANGE"
    assert report["data_quality"]["latest_feature_run"] is None
    assert report["readiness"] == EntryReadiness.READY  # 主链路不受影响


@pytest.mark.asyncio
async def test_aggregate_facts_no_action_candidate() -> None:
    security_id = uuid4()
    uow = _FactUow(_FakeDqReads(security_id), actions=_FakeActions([]))
    report = await _fact_service(security_id, uow).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["latest_action"]["status"] == "NOT_AVAILABLE"
    assert report["latest_action"]["reason"] == "NO_ACTION_CANDIDATE"
    assert report["latest_entry_assessment"]["reason"] == "NO_ENTRY_ASSESSMENT"


# ---------- R4-P0-001 PIT guard ----------


@pytest.mark.asyncio
async def test_future_decision_never_selected() -> None:
    """R4-P0-001 §26.2：as_of=T1 时未来 Decision 绝不入选——
    返回 T1 前已知的 Decision A，其计划同理。"""
    past_id, future_id = uuid4(), uuid4()
    bundle = {
        "decisions": (
            {"decision_id": past_id, "as_of": NOW - timedelta(days=1)},
            {"decision_id": future_id, "as_of": NOW + timedelta(days=1)},
        ),
        "entry_plan_versions": (
            {"entry_plan_id": uuid4(), "decision_id": future_id, "version": 2,
             "effective_from": NOW + timedelta(days=1),
             "expected_horizon": "D3_10", "plan": _PLAN},
            {"entry_plan_id": uuid4(), "decision_id": past_id, "version": 1,
             "effective_from": NOW - timedelta(days=1),
             "expected_horizon": "D3_10", "plan": _PLAN},
        ),
        "reviews": (),
    }
    report = await _service(_Quote(9.5), bundle).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["decision"]["decision_id"] == str(past_id)
    assert report["entry_plan"]["decision_id"] == str(past_id)


@pytest.mark.asyncio
async def test_future_effective_plan_never_selected() -> None:
    """R4-P0-001：effective_from > as_of 的 EntryPlan 绝不入选。"""
    plan_id = uuid4()
    bundle = {
        "decisions": ({"decision_id": plan_id, "as_of": NOW - timedelta(days=1)},),
        "entry_plan_versions": (
            {"entry_plan_id": uuid4(), "decision_id": plan_id, "version": 3,
             "effective_from": NOW + timedelta(days=1),
             "expected_horizon": "D3_10", "plan": _PLAN},
        ),
        "reviews": (),
    }
    report = await _service(_Quote(9.5), bundle).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["entry_plan"] is None
    assert report["readiness"] == EntryReadiness.NOT_READY
    assert report["readiness_reason"] == "NO_ENTRY_PLAN"


@pytest.mark.asyncio
async def test_unparseable_decision_time_never_selected() -> None:
    """时点不可解析的 Decision 绝不静默采纳。"""
    good_id, bad_id = uuid4(), uuid4()
    bundle = {
        "decisions": (
            {"decision_id": bad_id, "as_of": "not-a-time"},
            {"decision_id": good_id, "as_of": NOW - timedelta(days=1)},
        ),
        "entry_plan_versions": (
            {"entry_plan_id": uuid4(), "decision_id": good_id, "version": 1,
             "effective_from": NOW - timedelta(days=1),
             "expected_horizon": "D3_10", "plan": _PLAN},
        ),
        "reviews": (),
    }
    report = await _service(_Quote(9.5), bundle).execute(
        code="000001", market="SZ", as_of=NOW,
    )
    assert report["decision"]["decision_id"] == str(good_id)
