"""RC-08B READ Contract 真实 PostgreSQL 集成测试（API-002）。

整改方案 §11.2：补齐 GET /portfolio、/portfolio/{code}/reviews、
/portfolio/{code}/adjustments、/portfolio/preferences、
/entry-plans/{id}/versions、/watchlist/changes、/decisions、/reviews、
/market-reviews、/performance、/health/data-quality。
cases/similar 无真实相似度事实 → Product Backlog，不用伪算法硬补。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.read_operations import ReadOperationsService
from app.v3.infrastructure.db.models import (
    AccountModel,
    AIResultEnvelopeModel,
    DecisionModel,
    EntryPlanModel,
    MarketReviewModel,
    PortfolioAdjustmentModel,
    PortfolioPreferenceModel,
    PositionProjectionModel,
    PositionReviewModel,
    ReviewModel,
    WatchlistEventModel,
    WatchlistModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from tests.v3.test_deterministic_replay_postgres import _revision, _seed_chain

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 8, 30, 8, tzinfo=timezone.utc)


async def _seed_world(sessions):
    revision = _revision(uuid4())
    pack_id, envelope = await _seed_chain(sessions, revision)
    security_id = revision.security_id

    async def _envelope(result_type):
        row = AIResultEnvelopeModel(
            result_id=uuid4(), task_id=envelope.task_id,
            task_run_id=envelope.task_run_id, schema_version="v1",
            result_type=result_type, agent_type="AI", provider="OPENAI",
            model="test-model", context_pack_id=pack_id,
            context_pack_hash="0" * 64, prompt_version="p1",
            strategy_version="test-v1", produced_at=NOW, as_of=NOW,
            known_at=NOW, evidence_ids=[], payload={},
            content_hash=uuid4().hex,
        )
        return row

    async with SQLAlchemyUnitOfWork(sessions) as uow:
        session = uow._session
        decision_id = uuid4()
        session.add(DecisionModel(
            decision_id=decision_id, security_id=security_id,
            task_run_id=envelope.task_run_id, context_pack_id=pack_id,
            context_pack_hash="0" * 64, source_result_id=envelope.result_id,
            agent_identity={}, evidence_ids=[],
            original_entry_plan_snapshot={}, as_of=NOW, produced_at=NOW,
            payload={"direction": "LONG"}, content_hash=uuid4().hex,
        ))
        await session.flush()
        plan_v1_id, plan_v2_id = uuid4(), uuid4()
        session.add(EntryPlanModel(
            entry_plan_id=plan_v1_id, decision_id=decision_id, version=1,
            source_result_id=envelope.result_id, effective_from=NOW,
            expected_horizon="MID", plan={"entry": 10.0}, content_hash=uuid4().hex,
        ))
        await session.flush()
        session.add(EntryPlanModel(
            entry_plan_id=plan_v2_id, decision_id=decision_id, version=2,
            supersedes_entry_plan_id=plan_v1_id,
            source_result_id=envelope.result_id, effective_from=NOW,
            expected_horizon="MID", plan={"entry": 10.5}, content_hash=uuid4().hex,
        ))
        await session.flush()
        watchlist_id = uuid4()
        session.add(WatchlistModel(
            watchlist_id=watchlist_id, security_id=security_id,
            state="WATCHING", created_at=NOW, updated_at=NOW,
        ))
        await session.flush()
        session.add(WatchlistEventModel(
            event_id=uuid4(), watchlist_id=watchlist_id, from_state=None,
            to_state="WATCHING", reason="seed", event_time=NOW,
            content_hash=uuid4().hex,
        ))
        session.add(WatchlistEventModel(
            event_id=uuid4(), watchlist_id=watchlist_id, from_state="WATCHING",
            to_state="TRIGGERED", reason="seed2", event_time=NOW + timedelta(hours=1),
            content_hash=uuid4().hex,
        ))
        review_envelope = await _envelope("ReviewResult")
        session.add(review_envelope)
        session.add(ReviewModel(
            review_id=uuid4(), decision_id=decision_id,
            task_run_id=envelope.task_run_id, context_pack_id=pack_id,
            context_pack_hash="0" * 64, source_result_id=review_envelope.result_id,
            agent_identity={}, evidence_ids=[], thesis_status="INTACT",
            time_efficiency="NORMAL", as_of=NOW, payload={},
            content_hash=uuid4().hex,
        ))
        market_envelope = await _envelope("MarketReviewResult")
        session.add(market_envelope)
        session.add(MarketReviewModel(
            market_review_id=uuid4(), task_run_id=envelope.task_run_id,
            context_pack_id=pack_id, context_pack_hash="0" * 64,
            source_result_id=market_envelope.result_id, agent_identity={},
            evidence_ids=[], as_of=NOW, produced_at=NOW, payload={},
            content_hash=uuid4().hex,
        ))
        account_id = uuid4()
        session.add(AccountModel(
            account_id=account_id, name=f"acct-{uuid4().hex[:8]}",
            currency="CNY", cost_method="AVERAGE", status="ACTIVE",
        ))
        await session.flush()
        session.add(PositionProjectionModel(
            account_id=account_id, security_id=security_id,
            quantity=Decimal("100"), cost_basis=Decimal("1000"),
            average_cost=Decimal("10"), cash_impact=Decimal("-1000"),
            realized_pnl=Decimal("0"), last_ledger_sequence=1,
            last_adjustment_sequence=0, projection_version=1, rebuilt_at=NOW,
            input_hash="0" * 64,
        ))
        session.add(PortfolioAdjustmentModel(
            portfolio_adjustment_id=uuid4(), account_id=account_id,
            security_id=security_id, adjustment_type="SPLIT",
            effective_time=NOW, quantity_delta=Decimal("100"),
            cash_delta=Decimal("0"), cost_basis_delta=Decimal("0"),
            currency="CNY", source="CORPORATE_ACTION",
            source_reference=f"ref-{uuid4().hex[:8]}", known_at=NOW,
            confirmation_status="CONFIRMED", confirmed_by="op",
            confirmed_at=NOW, content_hash=uuid4().hex,
        ))
        session.add(PortfolioPreferenceModel(
            preference_id=uuid4(), account_id=account_id, version=1,
            preferences={"risk": "MEDIUM"}, effective_from=NOW,
            content_hash=uuid4().hex,
        ))
        position_envelope = await _envelope("PositionReviewResult")
        session.add(position_envelope)
        position_review_id = uuid4()
        session.add(PositionReviewModel(
            position_review_id=position_review_id, account_id=account_id,
            security_id=security_id, position_projection_hash="0" * 64,
            task_run_id=envelope.task_run_id, context_pack_id=pack_id,
            context_pack_hash="0" * 64,
            source_result_id=position_envelope.result_id, agent_identity={},
            evidence_ids=[], as_of=NOW, quantity_snapshot=Decimal("100"),
            average_cost_snapshot=Decimal("10"), thesis_status="INTACT",
            supporting_evidence={}, contrary_evidence={}, changed_facts={},
            new_risks=[], time_efficiency="NORMAL", recommended_action="HOLD",
            reason="seed", payload={},
            content_hash=uuid4().hex,
        ))
        from app.v3.infrastructure.db.models import PerformanceAttributionModel
        session.add(PerformanceAttributionModel(
            attribution_id=uuid4(), ability="SELECTION", subject_type="SECURITY",
            subject_id=security_id, decision_id=decision_id, horizon_sessions=10,
            strategy_version="test-v1", as_of=NOW,
            matures_at=NOW + timedelta(days=10),
            known_at=NOW + timedelta(days=11),
            metrics={"return": 0.05, "calculation_version":
                    "performance-mature-v1"}, explanation="seed",
            content_hash=uuid4().hex,
        ))
        await uow.commit()
    code = None
    async with sessions() as session:
        from app.v3.infrastructure.db.models import SecurityModel
        row = await session.get(SecurityModel, security_id)
        code = row.code
    return {
        "security_id": security_id, "code": code, "decision_id": decision_id,
        "plan_v1_id": plan_v1_id, "account_id": account_id,
        "watchlist_id": watchlist_id, "position_review_id": position_review_id,
        "pack_id": pack_id,
    }


async def test_all_read_contracts_return_immutable_facts() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    world = await _seed_world(sessions)
    service = ReadOperationsService(lambda: SQLAlchemyUnitOfWork(sessions))

    portfolio = await service.portfolio_overview()
    accounts = [
        item for item in portfolio["accounts"]
        if item["account_id"] == world["account_id"]
    ]
    assert len(accounts) == 1
    assert accounts[0]["positions"][0]["quantity"] == 100.0

    reviews = await service.position_reviews_by_code(world["code"], limit=10)
    assert [str(item["position_review_id"]) for item in reviews] == [
        str(world["position_review_id"])]
    assert reviews[0]["security_code"] == world["code"]
    assert reviews[0]["recommended_action"] == "HOLD"

    adjustments = await service.adjustments_by_code(world["code"], limit=10)
    assert [item["adjustment_type"] for item in adjustments] == ["SPLIT"]
    assert adjustments[0]["security_code"] == world["code"]

    preferences = [
        item for item in await service.preferences()
        if item["account_id"] == world["account_id"]
    ]
    assert len(preferences) == 1
    assert preferences[0]["preferences"] == {"risk": "MEDIUM"}

    versions = await service.entry_plan_versions(world["plan_v1_id"])
    assert [item["version"] for item in versions] == [1, 2]
    assert str(versions[1]["supersedes_entry_plan_id"]) == str(world["plan_v1_id"])

    changes = [
        item for item in await service.watchlist_changes(limit=200)
        if item["watchlist_id"] == world["watchlist_id"]
    ]
    assert [item["to_state"] for item in changes] == ["TRIGGERED", "WATCHING"]

    decisions = [
        item for item in await service.decisions(limit=200)
        if str(item["decision_id"]) == str(world["decision_id"])
    ]
    assert len(decisions) == 1

    reviews_all = [
        item for item in await service.reviews(limit=200)
        if str(item["decision_id"]) == str(world["decision_id"])
    ]
    assert len(reviews_all) == 1
    assert reviews_all[0]["thesis_status"] == "INTACT"

    market_reviews = await service.market_reviews(limit=10)
    assert len(market_reviews) >= 1

    performance = await service.performance(limit=200)
    own = [item for item in performance["attributions"]
           if str(item["decision_id"]) == str(world["decision_id"])]
    assert len(own) == 1
    assert own[0]["ability"] == "SELECTION"

    quality = await service.data_quality()
    assert quality["latest_feature_run"]["status"] == "PUBLISHED"
    assert quality["latest_feature_run"]["coverage"] == 1.0
    await engine.dispose()
