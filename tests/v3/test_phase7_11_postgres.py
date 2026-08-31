from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.contracts.agent import (
    AIResultEnvelope,
    AgentIdentity,
    AgentProvider,
    AgentType,
)
from app.v3.domain.performance import RegressionCaseCreate, ReplayRunCreate
from app.v3.domain.portfolio import (
    AccountCreate,
    OpeningPositionCreate,
    TradeConfirm,
    TradeDraftCreate,
    TradeSide,
)
from app.v3.domain.strategy import (
    ActorType,
    CapacityEvaluationCreate,
    ExperimentEventCommand,
    ExperimentType,
    GuardrailVersionCreate,
    OperationalHealthEventCreate,
    ReleaseMode,
    ShadowObservationCreate,
    StrategyActivationCommand,
    StrategyExperimentCreate,
    StrategyProposalCreate,
    StrategyVersionCreate,
)
from app.v3.infrastructure.db.models import SecurityModel
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.repositories.errors import RepositoryConflictError


DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_phase8_confirmed_ledger_serializes_concurrent_oversell() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account = AccountCreate(name=f"acceptance-{uuid4().hex}")
    security_id = uuid4()
    async with sessions() as session:
        session.add(
            SecurityModel(
                security_id=security_id,
                code=f"{security_id.int % 1_000_000:06d}",
                market="SH",
                name="phase8 acceptance",
            )
        )
        await session.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_account(account)
        await uow.portfolios.add_opening_position(
            OpeningPositionCreate(
                account_id=account.account_id,
                security_id=security_id,
                baseline_time=NOW,
                quantity=Decimal("100"),
                average_cost=Decimal("10"),
                source="ACCEPTANCE",
                confirmed_by="human",
            )
        )
        await uow.commit()
    drafts = []
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        for suffix in (1, 2):
            draft = TradeDraftCreate(
                account_id=account.account_id,
                security_id=security_id,
                side=TradeSide.SELL,
                trade_time=NOW + timedelta(minutes=suffix),
                price=Decimal("11"),
                quantity=Decimal("80"),
            )
            drafts.append(draft)
            await uow.portfolios.add_trade_draft(draft)
        await uow.commit()

    async def confirm(index: int):
        async with SQLAlchemyUnitOfWork(sessions) as uow:
            trade_id = await uow.portfolios.confirm_trade(
                drafts[index].draft_id,
                TradeConfirm(
                    idempotency_key=f"phase8-concurrent-{index:02d}",
                    confirmed_by="human",
                ),
            )
            await uow.commit()
            return trade_id

    results = await asyncio.gather(confirm(0), confirm(1), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], RepositoryConflictError)
    assert "oversell rejected" in str(failures[0])
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account.account_id, security_id)
    await engine.dispose()
    assert position is not None
    assert position["quantity"] == Decimal("20")


@pytest.mark.asyncio
async def test_phase10_missing_replay_input_blocks_regression_case() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    missing_revision = uuid4()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        replay = await uow.performance.run_replay(
            ReplayRunCreate(
                strategy_version=f"acceptance-{uuid4().hex}",
                replay_as_of=NOW,
                bar_revision_ids=(missing_revision,),
            )
        )
        await uow.commit()
    assert replay["status"] == "BLOCKED"
    assert replay["result"]["executed"] is False
    assert replay["leakage_checks"][0]["reason"] == "MISSING_INPUT"
    case = RegressionCaseCreate(
        name=f"blocked-{uuid4().hex}",
        strategy_version=replay["strategy_version"],
        replay_as_of=NOW,
        input_requirements={"bars": [str(missing_revision)]},
        expected_invariants={"no_lookahead": True},
        source_replay_run_id=replay["replay_run_id"],
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.performance.add_regression_case(case)
        await uow.commit()
    async with engine.connect() as connection:
        status = await connection.scalar(
            text(
                "SELECT status FROM v3.regression_cases "
                "WHERE regression_case_id=:id"
            ),
            {"id": case.regression_case_id},
        )
    await engine.dispose()
    assert status == "BLOCKED"


@pytest.mark.asyncio
async def test_phase9_manual_holding_review_needs_no_decision_and_creates_no_trade() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source_id = uuid4()
    snapshot_id = uuid4()
    feature_run_id = uuid4()
    profile_id = uuid4()
    context_id = uuid4()
    task_run_id = uuid4()
    task_id = uuid4()
    security_id = uuid4()
    fixture_now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    context_hash = uuid4().hex * 2
    def unique_hash() -> str:
        return uuid4().hex * 2
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.universe_sources "
                "(source_id, code, source_type, priority, capability_version) "
                "VALUES (:id, :code, 'OFFICIAL', 1, 'acceptance')"
            ),
            {"id": source_id, "code": f"p9-{uuid4().hex}"},
        )
        await connection.execute(
            text(
                "INSERT INTO v3.securities (security_id, code, market, name) "
                "VALUES (:id, :code, 'SH', 'phase9 manual holding')"
            ),
            {"id": security_id, "code": f"{security_id.int % 1_000_000:06d}"},
        )
        await connection.execute(
            text(
                "INSERT INTO v3.universe_snapshots "
                "(snapshot_id, source_id, as_of, fetch_time, known_at, coverage, "
                "stale, content_hash, status) VALUES "
                "(:id, :source, :now, :now, :now, 1, false, :hash, 'PRIMARY')"
            ),
            {
                "id": snapshot_id,
                "source": source_id,
                "now": fixture_now,
                "hash": unique_hash(),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO v3.universe_members "
                "(snapshot_id, security_id, name, trading_status, is_st, suspended, "
                "is_new_listing, delisting_risk, raw_reference) VALUES "
                "(:snapshot, :security, 'phase9 manual holding', 'ACTIVE', false, false, "
                "false, false, '{}'::jsonb)"
            ),
            {"snapshot": snapshot_id, "security": security_id},
        )
        await connection.execute(
            text(
                "INSERT INTO v3.feature_runs "
                "(feature_run_id, as_of, universe_snapshot_id, feature_version, status, "
                "expected_count, successful_count, failed_count, coverage, "
                "bar_revision_set_hash, input_manifest, error_summary, started_at, "
                "completed_at, content_hash) VALUES "
                "(:id, :now, :snapshot, 'acceptance', 'PUBLISHED', 0, 0, 0, 1, "
                ":bar_hash, '{}'::jsonb, '{}'::jsonb, :now, :now, :hash)"
            ),
            {
                "id": feature_run_id,
                "now": fixture_now,
                "snapshot": snapshot_id,
                "bar_hash": unique_hash(),
                "hash": unique_hash(),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO v3.task_profiles "
                "(task_profile_id, profile_code, version, timezone, trading_calendar, "
                "trading_calendar_source, trading_calendar_version, context_level, "
                "comparison_first, output_schema, expected_group_count, grace_seconds, "
                "strategy_version, enabled, content_hash) VALUES "
                "(:id, :code, 1, 'Asia/Shanghai', 'SSE', 'acceptance', '1', 'NORMAL', "
                "false, '{}'::jsonb, 1, 0, 'v3', true, :hash)"
            ),
            {"id": profile_id, "code": f"p9-{uuid4().hex}", "hash": unique_hash()},
        )
        await connection.execute(
            text(
                "INSERT INTO v3.context_packs "
                "(context_pack_id, context_level, subject_type, subject_id, task_profile_id, "
                "task_profile_version, builder_version, schema_version, as_of, known_at, "
                "universe_snapshot_id, feature_run_id, token_budget, actual_tokens, coverage, "
                "missing_fields, trim_summary, payload, \"references\", content_hash) VALUES "
                "(:id, 'NORMAL', 'SECURITY', :subject, :profile, 1, 'acceptance', 'v3', "
                ":now, :now, :snapshot, :feature, 5000, 1, 1, '[]'::jsonb, '{}'::jsonb, "
                "'{}'::jsonb, '[]'::jsonb, :hash)"
            ),
            {
                "id": context_id,
                "subject": str(security_id),
                "profile": profile_id,
                "now": fixture_now,
                "snapshot": snapshot_id,
                "feature": feature_run_id,
                "hash": context_hash,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO v3.task_runs "
                "(task_run_id, task_profile_id, task_profile_version, status, "
                "expected_group_count, successful_group_count, failed_group_count, "
                "pending_group_count, context_pack_id, context_pack_hash) VALUES "
                "(:id, :profile, 1, 'PENDING_IMPORT', 1, 0, 0, 1, :context, :hash)"
            ),
            {
                "id": task_run_id,
                "profile": profile_id,
                "context": context_id,
                "hash": context_hash,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO v3.agent_tasks "
                "(task_id, task_run_id, task_type, subject, task_profile, trigger_type, "
                "as_of, context_pack_id, context_pack_hash, expected_result_type, "
                "constraints, content_hash) VALUES "
                "(:id, :run, 'POSITION_REVIEW', '{}'::jsonb, 'NORMAL', 'USER_REQUEST', "
                ":now, :context, :hash, 'PositionReviewResult', '{}'::jsonb, :task_hash)"
            ),
            {
                "id": task_id,
                "run": task_run_id,
                "now": fixture_now,
                "context": context_id,
                "hash": context_hash,
                "task_hash": unique_hash(),
            },
        )
    account = AccountCreate(name=f"p9-{uuid4().hex}")
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.portfolios.add_account(account)
        await uow.portfolios.add_opening_position(
            OpeningPositionCreate(
                account_id=account.account_id,
                security_id=security_id,
                baseline_time=fixture_now,
                quantity=Decimal("100"),
                average_cost=Decimal("10"),
                source="MANUAL_BASELINE",
                confirmed_by="human",
            )
        )
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account.account_id, security_id)
    assert position is not None
    result_id = uuid4()
    envelope = AIResultEnvelope.build(
        {
            "result_id": result_id,
            "result_type": "PositionReviewResult",
            "agent": AgentIdentity(
                agent_type=AgentType.CHATGPT_WEB,
                provider=AgentProvider.OPENAI,
                model="acceptance",
            ),
            "task_id": task_id,
            "task_run_id": task_run_id,
            "task_profile": "NORMAL",
            "trigger_type": "USER_REQUEST",
            "context_pack_id": context_id,
            "context_pack_hash": context_hash,
            "prompt_version": "p9",
            "strategy_version": "v3",
            "produced_at": fixture_now,
            "as_of": fixture_now,
            "result": {
                "account_id": str(account.account_id),
                "security_id": str(security_id),
                "position_projection_hash": position["input_hash"],
                "recommended_action": "REDUCE",
                "reason": "manual holding review acceptance",
            },
        }
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.ai_result_envelopes "
                "(result_id, task_id, task_run_id, schema_version, result_type, agent_type, "
                "provider, model, context_pack_id, context_pack_hash, prompt_version, "
                "strategy_version, produced_at, as_of, known_at, evidence_ids, payload, "
                "content_hash) VALUES (:id, :task, :run, 'v3.0', 'PositionReviewResult', "
                "'CHATGPT_WEB', 'OPENAI', 'acceptance', :context, :context_hash, 'p9', 'v3', "
                ":now, :now, :now, '[]'::jsonb, :payload, :hash)"
            ),
            {
                "id": result_id,
                "task": task_id,
                "run": task_run_id,
                "context": context_id,
                "context_hash": context_hash,
                "now": fixture_now,
                "payload": json.dumps(envelope.model_dump(mode="json")),
                "hash": envelope.content_hash,
            },
        )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        created = await uow.ai_imports._materialize(envelope)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        reviews = await uow.actions.read_position_reviews(
            account.account_id, security_id, limit=10
        )
    async with engine.connect() as connection:
        trade_count = await connection.scalar(
            text(
                "SELECT count(*) FROM v3.trade_ledger "
                "WHERE account_id=:account AND security_id=:security"
            ),
            {"account": account.account_id, "security": security_id},
        )
    await engine.dispose()
    assert len(created) == 1
    assert len(reviews) == 1
    assert reviews[0]["decision_id"] is None
    assert reviews[0]["entry_plan_id"] is None
    assert reviews[0]["recommended_action"] == "REDUCE"
    assert trade_count == 0


@pytest.mark.asyncio
async def test_phase11_shadow_capacity_human_activation_and_automatic_rollback() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    strategy = StrategyVersionCreate(
        strategy_code=f"acceptance-{uuid4().hex}",
        version=1,
        configuration={"mode": "shadow"},
        rationale="phase11 acceptance",
        created_by="human",
    )
    guardrail = GuardrailVersionCreate(
        guardrail_code=f"acceptance-{uuid4().hex}",
        version=1,
        max_error_rate=0.1,
        max_p95_ms=100,
        min_shadow_sample_count=2,
        max_divergence_rate=0.5,
        max_capacity_utilization=0.8,
        created_by="human",
    )
    proposal = StrategyProposalCreate(
        proposed_strategy_version_id=strategy.strategy_version_id,
        actor_type=ActorType.HUMAN,
        actor_id="approver",
        hypothesis="safe shadow candidate",
        expected_improvements={"stability": True},
        created_at=NOW,
    )
    experiment = StrategyExperimentCreate(
        experiment_type=ExperimentType.SHADOW,
        treatment_strategy_version_id=strategy.strategy_version_id,
        guardrail_version_id=guardrail.guardrail_version_id,
        starts_at=NOW,
        created_by="human",
    )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.strategies.add_strategy_version(strategy)
        await uow.strategies.add_guardrail(guardrail)
        await uow.strategies.add_proposal(proposal)
        await uow.strategies.add_experiment(experiment)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.strategies.append_experiment_event(
            experiment.experiment_id,
            ExperimentEventCommand(
                event_type="STARTED",
                actor_type=ActorType.HUMAN,
                actor_id="operator",
                reason="acceptance start",
            ),
        )
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        first = await uow.strategies.assign_experiment(
            experiment.experiment_id, "600000"
        )
        second = await uow.strategies.assign_experiment(
            experiment.experiment_id, "600000"
        )
    assert first == second
    assert first["shadow_only"] is True
    assert first["assignment"] == "CONTROL"
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        for index, latency in enumerate((20, 40)):
            await uow.strategies.add_shadow_observation(
                ShadowObservationCreate(
                    experiment_id=experiment.experiment_id,
                    subject_key=f"stock-{index}",
                    observed_at=NOW + timedelta(minutes=index + 1),
                    control_output_hash="a" * 64,
                    treatment_output_hash="b" * 64,
                    control_payload={"decision": "OBSERVE"},
                    treatment_payload={"decision": "OBSERVE"},
                    materially_divergent=False,
                    latency_ms=latency,
                )
            )
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        capacity = await uow.strategies.evaluate_capacity(
            CapacityEvaluationCreate(
                strategy_version_id=strategy.strategy_version_id,
                guardrail_version_id=guardrail.guardrail_version_id,
                evaluated_at=NOW + timedelta(hours=1),
                capacity_utilization=0.5,
                provider_failures=0,
            )
        )
        await uow.commit()
    assert capacity["passed"] is True
    assert capacity["measured"] == {
        "sample_count": 2,
        "error_rate": 0.0,
        "p95_ms": 40.0,
        "divergence_rate": 0.0,
    }
    environment = f"acc-{uuid4().hex[:20]}"
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        activation = await uow.strategies.activate(
            environment,
            StrategyActivationCommand(
                proposal_id=proposal.proposal_id,
                strategy_version_id=strategy.strategy_version_id,
                guardrail_version_id=guardrail.guardrail_version_id,
                actor_type=ActorType.HUMAN,
                actor_id="approver",
                approval_reason="shadow only",
                target_mode=ReleaseMode.SHADOW,
                expected_row_version=0,
            ),
        )
        await uow.commit()
    assert activation["mode"] == "SHADOW"
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        health = await uow.strategies.add_health_event(
            OperationalHealthEventCreate(
                environment=environment,
                component="market-data",
                capability="quote",
                status="FAILED",
                error_type="UPSTREAM_UNAVAILABLE",
                circuit_state="OPEN",
                observed_at=NOW + timedelta(hours=2),
            )
        )
        await uow.commit()
    assert health["automatic_rollback_event_id"] is not None
    async with sessions() as session:
        state = await session.scalar(
            select(text("mode")).select_from(text("v3.release_states")).where(
                text("environment=:environment")
            ).params(environment=environment)
        )
    await engine.dispose()
    assert state == "V2"
