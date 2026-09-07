"""FC-01：active_price_trigger_plans 的 Trade-bound 绑定真实 PG 验收。

- 最新 BUY 被 REVERSE → 不充当绑定，回退上一笔有效 BUY（阻断点 2）；
- 多账户持有同一证券 → (account_id, security_id) 维度各自绑定
  （阻断点 2 多账户）；
- 持仓 + Watchlist 并存且无绑定 → 只出 ENTRY_WATCHLIST record，
  绝不合成为 DECISION binding 的复合来源（阻断点 1）。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.portfolio import (
    AccountCreate,
    EffectiveTradeState,
    TradeCorrectionStep,
)
from app.v3.infrastructure.db.decision_repositories import (
    _ACTIVE_WATCHLIST_STATES,
)
from app.v3.infrastructure.db.models import (
    AgentTaskModel,
    AIResultEnvelopeModel,
    ContextPackModel,
    DecisionModel,
    EntryPlanModel,
    FeatureRunModel,
    PositionProjectionModel,
    SecurityModel,
    TaskProfileModel,
    TaskRunModel,
    TradeCorrectionModel,
    TradeLedgerModel,
    UniverseSnapshotModel,
    UniverseSourceModel,
    WatchlistModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
PLAN = {"stop_loss": 9.0, "take_profit": 11.0}


@pytest.fixture(autouse=True)
async def _clean_business_tables():
    """active_price_trigger_plans 全局扫描持仓/绑定——残留数据会串场，
    每个用例前清空业务表（CASCADE 连带子表）。

    测试后同样清空：VerifyPositionProjections / Portfolio 派生任务同样
    全库扫描，本文件残留会污染 test_portfolio_jobs_postgres 等后续
    文件的 events_written 断言。"""
    from sqlalchemy import text

    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE v3.trade_corrections, v3.trade_ledger,"
            " v3.position_projections, v3.watchlists, v3.watchlist_events,"
            " v3.entry_plans, v3.decisions, v3.decision_corrections,"
            " v3.reviews, v3.accounts, v3.securities CASCADE"
        ))
    await engine.dispose()
    yield
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE v3.trade_corrections, v3.trade_ledger,"
            " v3.position_projections, v3.watchlists, v3.watchlist_events,"
            " v3.entry_plans, v3.decisions, v3.decision_corrections,"
            " v3.reviews, v3.accounts, v3.securities CASCADE"
        ))
    await engine.dispose()


async def _seed_security(sessions) -> tuple[object, object]:
    """播种 Security + 两个账户，返回 (security, [account_a, account_b])。"""
    from app.v3.infrastructure.db.models import AccountModel

    security_id = uuid4()
    account_a_id = uuid4()
    account_b_id = uuid4()
    async with sessions() as session:
        session.add(SecurityModel(
            security_id=security_id,
            code=f"{security_id.int % 1_000_000:06d}",
            market="SH", name="active plan acceptance",
        ))
        session.add(AccountModel(
            account_id=account_a_id, name=f"plan-a-{uuid4().hex[:12]}",
            currency="CNY", cost_method="AVERAGE", created_at=NOW,
        ))
        session.add(AccountModel(
            account_id=account_b_id, name=f"plan-b-{uuid4().hex[:12]}",
            currency="CNY", cost_method="AVERAGE", created_at=NOW,
        ))
        await session.commit()
    return security_id, [account_a_id, account_b_id]


async def _seed_decision_plan(sessions, security_id) -> object:
    """播种 Decision → EntryPlan 完整外键链，返回 entry_plan_id。"""
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
            effective_from=NOW, expected_horizon="D3_10", plan=dict(PLAN),
            content_hash=uuid4().hex,
        )
        for row in (source, profile, snapshot, task_run, feature_run,
                    context_pack, agent_task, envelope, decision, entry_plan):
            session.add(row)
            await session.flush()
        await session.commit()
    return entry_plan.entry_plan_id


def _projection(account_id, security_id) -> PositionProjectionModel:
    return PositionProjectionModel(
        account_id=account_id, security_id=security_id,
        quantity=Decimal("100"), cost_basis=Decimal("1000"),
        average_cost=Decimal("10"), cash_impact=Decimal("-1000"),
        realized_pnl=Decimal("0"), last_ledger_sequence=0,
        last_adjustment_sequence=0, projection_version=1,
        rebuilt_at=NOW, input_hash="0" * 64,
    )


def _buy_trade(account_id, security_id, entry_plan_id, *, trade_time) -> TradeLedgerModel:
    return TradeLedgerModel(
        trade_id=uuid4(), account_id=account_id, security_id=security_id,
        side="BUY", trade_time=trade_time, price=Decimal("10"),
        quantity=Decimal("100"), fee=Decimal("5"), source="TEST",
        decision_id=None, entry_plan_id=entry_plan_id, entry_plan_version=1,
        execution_deviation={}, idempotency_key=f"ik-{uuid4().hex}",
        confirmed_by="test", content_hash=uuid4().hex,
    )


def _reverse_correction(trade: TradeLedgerModel) -> TradeCorrectionModel:
    """从 DB 读回的 trade 构造 REVERSE correction——hash 链基于读回值
    （Numeric 精度 100.000000），与仓库读路径逐位一致。"""
    original = EffectiveTradeState(
        side=trade.side, quantity=trade.quantity,
        price=trade.price, fee=trade.fee,
    )
    step = TradeCorrectionStep.build(
        correction_type="REVERSE", replacement={}, previous_state=original,
    )
    return TradeCorrectionModel(
        correction_id=uuid4(), trade_id=trade.trade_id,
        correction_type=step.correction_type, replacement={},
        reason="test reverse", confirmed_by="test",
        previous_effective_hash=step.previous_effective_hash,
        effective_hash=step.effective_hash, content_hash=uuid4().hex,
    )


@pytest.mark.asyncio
async def test_reversed_buy_falls_back_to_earlier_effective_buy() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    security_id, (account_a, _) = await _seed_security(sessions)
    plan_old = await _seed_decision_plan(sessions, security_id)
    plan_new = await _seed_decision_plan(sessions, security_id)
    trade_old = _buy_trade(
        account_a, security_id, plan_old, trade_time=NOW,
    )
    trade_new = _buy_trade(
        account_a, security_id, plan_new, trade_time=NOW + timedelta(hours=1),
    )
    async with sessions() as session:
        session.add(_projection(account_a, security_id))
        session.add(trade_old)
        session.add(trade_new)
        await session.commit()
    # 读回 trade（Numeric 精度与仓库读路径一致）再算 REVERSE hash 链
    from sqlalchemy import select as _select

    async with sessions() as session:
        rows = (
            await session.scalars(
                _select(TradeLedgerModel).where(
                    TradeLedgerModel.security_id == security_id
                )
            )
        ).all()
        trade_new = next(t for t in rows if t.entry_plan_id == plan_new)
        session.add(_reverse_correction(trade_new))
        await session.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        rows = await uow.ai_imports.active_price_trigger_plans()
    bound = [r for r in rows if r["plan_binding"] == "TRADE_BOUND"
             and r["security_id"] == security_id]
    assert len(bound) == 1
    # 最新 BUY 已 REVERSE → 绑定回退上一笔有效 BUY（plan_old）
    assert bound[0]["entry_plan_id"] == plan_old
    assert bound[0]["account_id"] == account_a
    await engine.dispose()


@pytest.mark.asyncio
async def test_multi_account_each_binds_own_plan() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    security_id, (account_a, account_b) = await _seed_security(sessions)
    plan_a = await _seed_decision_plan(sessions, security_id)
    plan_b = await _seed_decision_plan(sessions, security_id)
    async with sessions() as session:
        session.add(_projection(account_a, security_id))
        session.add(_projection(account_b, security_id))
        session.add(_buy_trade(
            account_a, security_id, plan_a, trade_time=NOW,
        ))
        session.add(_buy_trade(
            account_b, security_id, plan_b, trade_time=NOW,
        ))
        await session.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        rows = await uow.ai_imports.active_price_trigger_plans()
    bound = [r for r in rows if r["plan_binding"] == "TRADE_BOUND"
             and r["security_id"] == security_id]
    assert len(bound) == 2  # 不压缩成证券级单一 Plan
    by_account = {r["account_id"]: r for r in bound}
    assert by_account[account_a]["entry_plan_id"] == plan_a
    assert by_account[account_b]["entry_plan_id"] == plan_b
    assert all(r["plan_source"] == "POSITION" for r in bound)
    await engine.dispose()


@pytest.mark.asyncio
async def test_position_plus_watchlist_without_binding_is_watchlist_only() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    security_id, (account_a, _) = await _seed_security(sessions)
    plan = await _seed_decision_plan(sessions, security_id)
    async with sessions() as session:
        session.add(_projection(account_a, security_id))
        session.add(WatchlistModel(
            watchlist_id=uuid4(), security_id=security_id,
            state=_ACTIVE_WATCHLIST_STATES[0],
            created_at=NOW, updated_at=NOW,
        ))
        await session.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        rows = await uow.ai_imports.active_price_trigger_plans()
    rows = [r for r in rows if r["security_id"] == security_id]
    assert len(rows) == 1  # 绝不合成 ENTRY_WATCHLIST+POSITION + DECISION
    assert rows[0]["plan_source"] == "ENTRY_WATCHLIST"
    assert rows[0]["plan_binding"] == "DECISION"
    assert rows[0]["entry_plan_id"] == plan
    assert rows[0]["account_id"] is None
    await engine.dispose()
