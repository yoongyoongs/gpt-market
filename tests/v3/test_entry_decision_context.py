"""RT-06：Entry Decision Context + Typed EntryPlanPayload（实时方案 §8.4/§17.2/§27 RT-06）。

- EntryPlanPayload 类型化：不同 AI Result 绝不各写各的字段；
- Trigger / Cancel 客观评估：只做确定性判断（价格条件），TEXT 条件不可客观评估；
- 验收：能回答"现在能买吗"；缺关键实时数据（quote 缺失/stale/无价格）绝不 READY。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
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
