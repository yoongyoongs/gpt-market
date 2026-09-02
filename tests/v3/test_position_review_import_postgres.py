"""RT-07：PositionReview 导入收口（真实 PG）。

- SELL 兼容映射 → 库里只存 EXIT；
- REDUCE 缺 reduce_ratio → 拒绝（RepositoryConflictError）；
- Position Review 绝不创建 Trade（trade_ledger 行数为 0）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.domain.ai_import import AIResultEnvelope
from app.v3.contracts.agent import AgentIdentity, AgentProvider, AgentType
from app.v3.domain.portfolio import AccountCreate, OpeningPositionCreate
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.errors import RepositoryConflictError

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)

fixture_now = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)


def unique_hash() -> str:
    return uuid4().hex + uuid4().hex


async def _setup(engine) -> dict:
    """最小可导入环境：security/universe/feature/context/task/持仓。"""
    security_id = uuid4()
    account_id = uuid4()
    snapshot_id = uuid4()
    source_id = uuid4()
    feature_run_id = uuid4()
    profile_id = uuid4()
    context_id = uuid4()
    context_hash = unique_hash()
    task_run_id = uuid4()
    task_id = uuid4()
    code = f"{security_id.int % 1_000_000:06d}"
    now = fixture_now
    async with engine.begin() as connection:
        statements = [
            ("INSERT INTO v3.universe_sources (source_id, code, source_type, priority, capability_version) VALUES (:s, :c, 'OFFICIAL', 1, 't')",
             {"s": source_id, "c": f"p7-{uuid4().hex}"}),
            ("INSERT INTO v3.securities (security_id, code, market, name) VALUES (:id, :code, 'SH', 'rt07')",
             {"id": security_id, "code": code}),
            ("INSERT INTO v3.universe_snapshots (snapshot_id, source_id, as_of, fetch_time, known_at, coverage, stale, content_hash, status) VALUES (:id, :s, :now, :now, :now, 1, false, :h, 'PRIMARY')",
             {"id": snapshot_id, "s": source_id, "now": now, "h": unique_hash()}),
            ("INSERT INTO v3.universe_members (snapshot_id, security_id, name, trading_status, is_st, suspended, is_new_listing, delisting_risk, raw_reference) VALUES (:sn, :id, 'rt07', 'ACTIVE', false, false, false, false, '{}'::jsonb)",
             {"sn": snapshot_id, "id": security_id}),
            ("INSERT INTO v3.feature_runs (feature_run_id, as_of, universe_snapshot_id, feature_version, status, expected_count, successful_count, failed_count, coverage, bar_revision_set_hash, input_manifest, error_summary, started_at, completed_at, content_hash) VALUES (:id, :now, :sn, 't', 'PUBLISHED', 0, 0, 0, 1, :bh, '{}'::jsonb, '{}'::jsonb, :now, :now, :h)",
             {"id": feature_run_id, "now": now, "sn": snapshot_id, "bh": unique_hash(), "h": unique_hash()}),
            ("INSERT INTO v3.task_profiles (task_profile_id, profile_code, version, timezone, trading_calendar, trading_calendar_source, trading_calendar_version, context_level, comparison_first, output_schema, expected_group_count, grace_seconds, strategy_version, enabled, content_hash) VALUES (:id, :code, 1, 'Asia/Shanghai', 'SSE', 't', '1', 'NORMAL', false, '{}'::jsonb, 1, 0, 'v3', true, :h)",
             {"id": profile_id, "code": f"p7-{uuid4().hex}", "h": unique_hash()}),
            ("INSERT INTO v3.context_packs (context_pack_id, context_level, subject_type, subject_id, task_profile_id, task_profile_version, builder_version, schema_version, as_of, known_at, universe_snapshot_id, feature_run_id, token_budget, actual_tokens, coverage, missing_fields, trim_summary, payload, \"references\", content_hash) VALUES (:id, 'NORMAL', 'SECURITY', :subject, :profile, 1, 't', 'v3', :now, :now, :sn, :fr, 5000, 1, 1, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, :h)",
             {"id": context_id, "subject": str(security_id), "profile": profile_id, "now": now, "sn": snapshot_id, "fr": feature_run_id, "h": context_hash}),
            ("INSERT INTO v3.task_runs (task_run_id, task_profile_id, task_profile_version, status, expected_group_count, successful_group_count, failed_group_count, pending_group_count, context_pack_id, context_pack_hash) VALUES (:id, :profile, 1, 'PENDING_IMPORT', 1, 0, 0, 1, :ctx, :h)",
             {"id": task_run_id, "profile": profile_id, "ctx": context_id, "h": context_hash}),
            ("INSERT INTO v3.agent_tasks (task_id, task_run_id, task_type, subject, task_profile, trigger_type, as_of, context_pack_id, context_pack_hash, expected_result_type, constraints, content_hash) VALUES (:id, :run, 'POSITION_REVIEW', '{}'::jsonb, 'NORMAL', 'USER_REQUEST', :now, :ctx, :ch, 'PositionReviewResult', '{}'::jsonb, :th)",
             {"id": task_id, "run": task_run_id, "now": now, "ctx": context_id, "ch": context_hash, "th": unique_hash()}),
        ]
        for statement, params in statements:
            await connection.execute(text(statement), params)
    async with SQLAlchemyUnitOfWork(async_sessionmaker(engine, expire_on_commit=False)) as uow:
        await uow.portfolios.add_account(AccountCreate(
            account_id=account_id, name=f"rt07-{uuid4().hex}",
        ))
        await uow.portfolios.add_opening_position(OpeningPositionCreate(
            account_id=account_id, security_id=security_id,
            baseline_time=fixture_now, quantity=Decimal("100"),
            average_cost=Decimal("10"), source="MANUAL_BASELINE",
            confirmed_by="human",
        ))
        await uow.commit()
    return {
        "security_id": security_id, "account_id": account_id,
        "task_id": task_id, "task_run_id": task_run_id,
        "context_id": context_id, "context_hash": context_hash,
    }


def _envelope(fixture: dict, result: dict) -> AIResultEnvelope:
    return AIResultEnvelope.build({
        "result_id": uuid4(),
        "result_type": "PositionReviewResult",
        "agent": AgentIdentity(
            agent_type=AgentType.CHATGPT_WEB,
            provider=AgentProvider.OPENAI, model="rt07",
        ),
        "task_id": fixture["task_id"],
        "task_run_id": fixture["task_run_id"],
        "task_profile": "NORMAL",
        "trigger_type": "USER_REQUEST",
        "context_pack_id": fixture["context_id"],
        "context_pack_hash": fixture["context_hash"],
        "prompt_version": "rt07",
        "strategy_version": "v3",
        "produced_at": fixture_now,
        "as_of": fixture_now,
        "result": result,
    })


@pytest.mark.asyncio
async def test_sell_action_imported_as_exit_without_trade() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _setup(engine)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(
            fixture["account_id"], fixture["security_id"]
        )
    envelope = _envelope(fixture, {
        "account_id": str(fixture["account_id"]),
        "security_id": str(fixture["security_id"]),
        "position_projection_hash": position["input_hash"],
        "recommended_action": "SELL",  # 兼容层：必须映射为 EXIT
        "reason": "兼容旧 AI 输出",
    })
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.ai_result_envelopes "
                "(result_id, task_id, task_run_id, schema_version, result_type, agent_type, "
                "provider, model, context_pack_id, context_pack_hash, prompt_version, "
                "strategy_version, produced_at, as_of, known_at, evidence_ids, payload, "
                "content_hash) VALUES (:id, :task, :run, 'v3.0', 'PositionReviewResult', "
                "'CHATGPT_WEB', 'OPENAI', 'rt07', :ctx, :ch, 'rt07', 'v3', "
                ":now, :now, :now, '[]'::jsonb, :payload, :hash)"
            ),
            {
                "id": envelope.result_id, "task": envelope.task_id,
                "run": envelope.task_run_id, "ctx": fixture["context_id"],
                "ch": fixture["context_hash"], "now": fixture_now,
                "payload": json.dumps(envelope.model_dump(mode="json")),
                "hash": envelope.content_hash,
            },
        )
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        created = await uow.ai_imports._materialize(envelope)
        await uow.commit()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        reviews = await uow.actions.read_position_reviews(
            fixture["account_id"], fixture["security_id"], limit=10
        )
    async with engine.connect() as connection:
        trade_count = await connection.scalar(text(
            "SELECT count(*) FROM v3.trade_ledger "
            "WHERE account_id=:account AND security_id=:security"
        ), {"account": fixture["account_id"], "security": fixture["security_id"]})
    await engine.dispose()
    assert len(created) == 1
    assert reviews[0]["recommended_action"] == "EXIT"
    assert trade_count == 0


@pytest.mark.asyncio
async def test_reduce_without_ratio_rejected() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _setup(engine)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(
            fixture["account_id"], fixture["security_id"]
        )
    envelope = _envelope(fixture, {
        "account_id": str(fixture["account_id"]),
        "security_id": str(fixture["security_id"]),
        "position_projection_hash": position["input_hash"],
        "recommended_action": "REDUCE",
        "reason": "缺比例建议，必须拒绝",
    })
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        with pytest.raises(RepositoryConflictError):
            await uow.ai_imports._materialize(envelope)
    await engine.dispose()
