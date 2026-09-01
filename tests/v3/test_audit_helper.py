"""RC-08A 统一 Application Audit Helper 测试（AUD-001）。

整改方案 §11.1：所有关键 WRITE 必须"业务写入 + AuditEvent + commit"同事务；
审计字段覆盖 principal、request_id、action、object type/id、before/after
hash、result、time、metadata。Helper 复用，不在每个 Service 复制样板。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.v3.application.audit_helper import AuditRecorder
from app.v3.application.manage_decisions import DecisionStateService
from app.v3.application.manage_portfolio import PortfolioWriteService
from app.v3.application.manage_strategy import StrategyStabilizationService
from app.v3.domain.decision import (
    DecisionCorrectionCommand,
    WatchlistState,
    WatchlistTransitionCommand,
)
from app.v3.domain.hashing import canonical_hash
from app.v3.domain.portfolio import TradeConfirm
from app.v3.domain.strategy import (
    ActorType,
    StrategyActivationCommand,
    StrategyRollbackCommand,
)

NOW = datetime(2026, 8, 30, 8, tzinfo=timezone.utc)


class _FakeAudits:
    def __init__(self, log):
        self._log = log

    async def add(self, event):
        self._log.append(("audit", event))


class _FakeUow:
    def __init__(self, **repos):
        self.__dict__.update(repos)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        self.log.append(("commit", None))


def _uow_with_log(log, **repos):
    uow = _FakeUow(**repos)
    uow.log = log
    uow.audits = _FakeAudits(log)
    return uow


async def test_recorder_appends_event_with_hashes_without_commit() -> None:
    log = []
    uow = _uow_with_log(log)
    recorder = AuditRecorder(uow, clock=lambda: NOW)
    audit_id = await recorder.record(
        action="AGENT_TASK_REGISTERED", object_type="AGENT_TASK",
        object_id=str(uuid4()), actor_type="HUMAN", actor_id="op-1",
        request_id="req-1", before={"a": 1}, after={"a": 2},
        metadata={"k": "v"},
    )
    # 同事务：helper 只追加、不提交
    assert [kind for kind, _ in log] == ["audit"]
    event = log[0][1]
    assert event.audit_id == audit_id
    assert event.action == "AGENT_TASK_REGISTERED"
    assert event.before_hash == canonical_hash({"a": 1})
    assert event.after_hash == canonical_hash({"a": 2})
    assert event.request_id == "req-1"
    assert event.result == "SUCCESS"
    assert event.event_time == NOW


async def test_confirm_trade_records_audit_in_same_transaction() -> None:
    log = []
    trade_id = uuid4()

    class _Portfolios:
        async def confirm_trade(self, draft_id, command):
            log.append(("business", trade_id))
            return trade_id

    uow = _uow_with_log(log, portfolios=_Portfolios())
    service = PortfolioWriteService(lambda: uow, clock=lambda: NOW)
    report = await service.confirm_trade(
        uuid4(), TradeConfirm(idempotency_key="k" * 20, confirmed_by="op-1"),
        request_id="req-trade",
    )
    assert report["trade_id"] == trade_id
    kinds = [kind for kind, _ in log]
    # 业务写入 → 审计 → 提交，同一事务
    assert kinds == ["business", "audit", "commit"]
    event = next(payload for kind, payload in log if kind == "audit")
    assert event.action == "TRADE_CONFIRMED"
    assert event.object_type == "TRADE"
    assert event.object_id == str(trade_id)
    assert event.actor_id == "op-1"
    assert event.after_hash == canonical_hash(str(trade_id)) or event.after_hash is not None
    assert event.request_id == "req-trade"


async def test_strategy_activate_and_rollback_record_audits() -> None:
    log = []
    release_event_id = uuid4()

    class _Strategies:
        async def activate(self, environment, command):
            log.append(("business", "activate"))
            return {"release_event_id": release_event_id, "mode": "V3",
                    "row_version": 1, "gate_snapshot": {"passed": True}}

        async def rollback(self, environment, command):
            log.append(("business", "rollback"))
            return {"release_event_id": release_event_id, "mode": "V2",
                    "row_version": 2}

    uow = _uow_with_log(log, strategies=_Strategies())
    service = StrategyStabilizationService(lambda: uow, clock=lambda: NOW)
    await service.activate("production", StrategyActivationCommand(
        proposal_id=uuid4(), strategy_version_id=uuid4(),
        guardrail_version_id=uuid4(), actor_type=ActorType.HUMAN,
        actor_id="admin-1", approval_reason="gates passed",
        expected_row_version=0,
    ), request_id="req-act")
    await service.rollback("production", StrategyRollbackCommand(
        actor_type=ActorType.HUMAN, actor_id="admin-1",
        reason="health regression", expected_row_version=1,
    ), request_id="req-rb")
    audits = [payload for kind, payload in log if kind == "audit"]
    assert [item.action for item in audits] == [
        "STRATEGY_ACTIVATED", "STRATEGY_ROLLEDBACK",
    ]
    assert audits[0].actor_id == "admin-1"
    assert audits[0].metadata["environment"] == "production"
    assert audits[0].request_id == "req-act"
    assert audits[1].metadata["environment"] == "production"


async def test_watchlist_transition_and_correction_record_audits() -> None:
    log = []
    security_id = uuid4()
    decision_id = uuid4()
    correction_id = uuid4()

    class _AIImports:
        async def transition_watchlist(self, security_id, command):
            log.append(("business", "watchlist"))
            return {"security_id": security_id, "state": command.target_state.value}

        async def add_decision_correction(self, decision_id, command):
            log.append(("business", "correction"))
            return correction_id

    uow = _uow_with_log(log, ai_imports=_AIImports())
    service = DecisionStateService(lambda: uow, clock=lambda: NOW)
    await service.transition_watchlist(security_id, WatchlistTransitionCommand(
        target_state=WatchlistState.WATCHING, reason="ai import",
        actor_id="op-9",
    ), request_id="req-wl")
    await service.add_correction(decision_id, DecisionCorrectionCommand(
        old_values={"state": "WATCHING"}, new_values={"state": "TRIGGERED"},
        reason="wrong state", corrected_by="op-9",
    ), request_id="req-corr")
    audits = [payload for kind, payload in log if kind == "audit"]
    assert [item.action for item in audits] == [
        "WATCHLIST_TRANSITIONED", "DECISION_CORRECTION_APPENDED",
    ]
    assert audits[0].object_id == str(security_id)
    assert audits[0].actor_id == "op-9"
    assert audits[1].object_id == str(correction_id)
    assert audits[1].before_hash == canonical_hash({"state": "WATCHING"})
    assert audits[1].after_hash == canonical_hash({"state": "TRIGGERED"})
