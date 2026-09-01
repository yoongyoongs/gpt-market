from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.hashing import canonical_hash
from app.v3.domain.portfolio import (
    AccountCreate,
    TradeConfirm,
    TradeDraftCreate,
    TradeSide,
)
from app.v3.infrastructure.db.models import (
    AgentTaskModel,
    AIResultEnvelopeModel,
    ContextPackModel,
    DecisionModel,
    EntryPlanModel,
    FeatureRunModel,
    SecurityModel,
    TradeLedgerModel,
    TaskProfileModel,
    TaskRunModel,
    UniverseSnapshotModel,
    UniverseSourceModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)

PLAN = {
    "entry_price_low": "10",
    "entry_price_high": "11",
    "quantity": "100",
    "entry_window_start": "2026-09-01T01:30:00+00:00",
    "entry_window_end": "2026-09-01T02:30:00+00:00",
    "trigger": {"type": "PRICE_ABOVE", "price": "10.5"},
    "cancel_condition": {"type": "PRICE_BELOW", "price": "9.5"},
}


async def _seed_decision_chain(sessions, security_id: UUID) -> UUID:
    """播种 Security → Universe → Feature → Profile → TaskRun → ContextPack
    → AgentTask → Envelope → Decision → EntryPlan 完整外键链。"""
    async with sessions() as session:
        source = UniverseSourceModel(
            source_id=uuid4(), code=f"src-{uuid4().hex[:16]}",
            source_type="EXCHANGE", priority=1, capability_version="1",
        )
        snapshot = UniverseSnapshotModel(
            snapshot_id=uuid4(), source_id=source.source_id,
            as_of=NOW, fetch_time=NOW, known_at=NOW,
            coverage=Decimal("1"), stale=False, status="PRIMARY",
            content_hash=uuid4().hex,
        )
        feature_run = FeatureRunModel(
            feature_run_id=uuid4(), as_of=NOW,
            universe_snapshot_id=snapshot.snapshot_id, feature_version="v1",
            status="PUBLISHED", expected_count=1, coverage=Decimal("1"),
            bar_revision_set_hash="0" * 64, input_manifest={}, started_at=NOW,
        )
        profile = TaskProfileModel(
            task_profile_id=uuid4(), profile_code=f"profile-{uuid4().hex[:16]}",
            version=1, timezone="Asia/Shanghai", context_level="NORMAL",
            output_schema={}, content_hash=uuid4().hex,
        )
        task_run = TaskRunModel(
            task_run_id=uuid4(), task_profile_id=profile.task_profile_id,
            expected_group_count=1, pending_group_count=1,
        )
        context_pack = ContextPackModel(
            context_pack_id=uuid4(), context_level="NORMAL",
            subject_type="SECURITY", subject_id="600000",
            task_profile_id=profile.task_profile_id, task_profile_version=1,
            builder_version="v1", schema_version="v1",
            as_of=NOW, known_at=NOW,
            universe_snapshot_id=snapshot.snapshot_id,
            feature_run_id=feature_run.feature_run_id,
            token_budget=5000, actual_tokens=100, coverage=Decimal("1"),
            missing_fields=[], trim_summary={}, payload={}, references=[],
            content_hash=uuid4().hex,
        )
        agent_task = AgentTaskModel(
            task_id=uuid4(), task_run_id=task_run.task_run_id,
            task_type="STOCK_ANALYSIS", subject={}, task_profile="TEST_PROFILE",
            trigger_type="SCHEDULE", as_of=NOW,
            context_pack_id=context_pack.context_pack_id,
            context_pack_hash="0" * 64, expected_result_type="DecisionResult",
            content_hash=uuid4().hex,
        )
        envelope = AIResultEnvelopeModel(
            result_id=uuid4(), task_id=agent_task.task_id,
            task_run_id=task_run.task_run_id, schema_version="v1",
            result_type="DecisionResult", agent_type="AI", provider="OPENAI",
            model="test-model", context_pack_id=context_pack.context_pack_id,
            context_pack_hash="0" * 64, prompt_version="p1",
            strategy_version="v1", produced_at=NOW, as_of=NOW, known_at=NOW,
            evidence_ids=[], payload={}, content_hash=uuid4().hex,
        )
        decision = DecisionModel(
            decision_id=uuid4(), security_id=security_id,
            task_run_id=task_run.task_run_id,
            context_pack_id=context_pack.context_pack_id,
            context_pack_hash="0" * 64, source_result_id=envelope.result_id,
            agent_identity={}, evidence_ids=[],
            original_entry_plan_snapshot={}, as_of=NOW, produced_at=NOW,
            payload={}, content_hash=uuid4().hex,
        )
        entry_plan = EntryPlanModel(
            entry_plan_id=uuid4(), decision_id=decision.decision_id,
            version=1, source_result_id=envelope.result_id,
            effective_from=NOW, expected_horizon="D3_10", plan=PLAN,
            content_hash=uuid4().hex,
        )
        for row in (source, profile, snapshot, task_run, feature_run,
                    context_pack, agent_task, envelope, decision, entry_plan):
            session.add(row)
            await session.flush()
        await session.commit()
    return entry_plan.entry_plan_id


@pytest.mark.asyncio
async def test_plan_bound_trade_persists_full_execution_deviation() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    security_id = uuid4()
    account = AccountCreate(name=f"deviation-{uuid4().hex}")
    async with sessions() as session:
        session.add(SecurityModel(
            security_id=security_id,
            code=f"{security_id.int % 1_000_000:06d}",
            market="SH", name="deviation acceptance",
        ))
        await session.commit()
    entry_plan_id = await _seed_decision_chain(sessions, security_id)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_account(account)
        await uow.commit()
    trigger_facts = {"price": "10.6"}
    draft = TradeDraftCreate(
        account_id=account.account_id, security_id=security_id,
        side=TradeSide.BUY, trade_time=NOW, price=Decimal("10.5"),
        quantity=Decimal("100"), entry_plan_id=entry_plan_id,
        entry_plan_version=1, trigger_facts=trigger_facts,
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_trade_draft(draft)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        trade_id = await uow.portfolios.confirm_trade(
            draft.draft_id,
            TradeConfirm(
                idempotency_key=f"deviation-{uuid4().hex}",
                confirmed_by="acceptance-human",
            ),
        )
        await uow.commit()
    async with sessions() as session:
        ledger = await session.scalar(
            select(TradeLedgerModel).where(TradeLedgerModel.trade_id == trade_id)
        )
    await engine.dispose()
    assert ledger is not None
    deviation = ledger.execution_deviation
    assert deviation["price_window_relation"] == "INSIDE"
    assert deviation["price_delta_pct"] == "0"
    assert deviation["time_window_relation"] == "INSIDE"
    assert deviation["session_delta_minutes"] == "0"
    assert deviation["quantity_delta"] == "0"
    assert deviation["quantity_delta_pct"] == "0"
    assert deviation["trigger_match"] == "MATCH"
    assert deviation["cancel_condition_violated"] == "NOT_VIOLATED"
    assert deviation["trigger_facts_hash"] == canonical_hash(trigger_facts)
    assert deviation["plan_snapshot_hash"] == canonical_hash(PLAN)
    assert ledger.entry_plan_id == entry_plan_id
    assert ledger.entry_plan_version == 1
