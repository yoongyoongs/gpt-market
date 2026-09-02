"""RT-09：AI Result Import 收口（实时方案 §27 RT-09）。

- Entry/Review Schema：EntryPlanResult 落库前必须通过 EntryPlanPayload
  类型化校验，结构非法拒绝；
- Audit：preview / confirm / group commit 留 AuditEvent 审计链；
- Idempotency：同一 import 重复 confirm 不重复落库；
- 不能写真实 Trade：任何 AI Result 导入绝不产生 trade_ledger 行。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.contracts.agent import AgentIdentity, AgentProvider, AgentType
from app.v3.domain.ai_import import (
    AIResultBundle,
    AIResultConfirmCommand,
    AIResultEnvelope,
)
from app.v3.application.import_ai_results import (
    ConfirmAIResultImportService,
    PreviewAIResultImportService,
)
from app.v3.errors import RepositoryConflictError
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)

fixture_now = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)


def unique_hash() -> str:
    return uuid4().hex + uuid4().hex


async def _setup(engine, expected_result_type: str) -> dict:
    """最小可导入环境（同 RT-07 harness，result_type 参数化）。"""
    security_id = uuid4()
    snapshot_id = uuid4()
    source_id = uuid4()
    feature_run_id = uuid4()
    profile_id = uuid4()
    context_id = uuid4()
    context_hash = unique_hash()
    task_run_id = uuid4()
    task_id = uuid4()
    now = fixture_now
    async with engine.begin() as connection:
        statements = [
            (
                "INSERT INTO v3.universe_sources (source_id, code, source_type, priority, capability_version) VALUES (:s, :c, 'OFFICIAL', 1, 't')",
                {"s": source_id, "c": f"rt09-{uuid4().hex}"},
            ),
            (
                "INSERT INTO v3.securities (security_id, code, market, name) VALUES (:id, :code, 'SZ', 'rt09')",
                {"id": security_id, "code": f"{security_id.int % 1_000_000:06d}"},
            ),
            (
                "INSERT INTO v3.universe_snapshots (snapshot_id, source_id, as_of, fetch_time, known_at, coverage, stale, content_hash, status) VALUES (:id, :s, :now, :now, :now, 1, false, :h, 'PRIMARY')",
                {"id": snapshot_id, "s": source_id, "now": now, "h": unique_hash()},
            ),
            (
                "INSERT INTO v3.universe_members (snapshot_id, security_id, name, trading_status, is_st, suspended, is_new_listing, delisting_risk, raw_reference) VALUES (:sn, :id, 'rt09', 'ACTIVE', false, false, false, false, '{}'::jsonb)",
                {"sn": snapshot_id, "id": security_id},
            ),
            (
                "INSERT INTO v3.feature_runs (feature_run_id, as_of, universe_snapshot_id, feature_version, status, expected_count, successful_count, failed_count, coverage, bar_revision_set_hash, input_manifest, error_summary, started_at, completed_at, content_hash) VALUES (:id, :now, :sn, 't', 'PUBLISHED', 0, 0, 0, 1, :bh, '{}'::jsonb, '{}'::jsonb, :now, :now, :h)",
                {
                    "id": feature_run_id,
                    "now": now,
                    "sn": snapshot_id,
                    "bh": unique_hash(),
                    "h": unique_hash(),
                },
            ),
            (
                "INSERT INTO v3.task_profiles (task_profile_id, profile_code, version, timezone, trading_calendar, trading_calendar_source, trading_calendar_version, context_level, comparison_first, output_schema, expected_group_count, grace_seconds, strategy_version, enabled, content_hash) VALUES (:id, :code, 1, 'Asia/Shanghai', 'SSE', 't', '1', 'NORMAL', false, '{}'::jsonb, 1, 0, 'v3', true, :h)",
                {"id": profile_id, "code": f"rt09-{uuid4().hex}", "h": unique_hash()},
            ),
            (
                "INSERT INTO v3.context_packs (context_pack_id, context_level, subject_type, subject_id, task_profile_id, task_profile_version, builder_version, schema_version, as_of, known_at, universe_snapshot_id, feature_run_id, token_budget, actual_tokens, coverage, missing_fields, trim_summary, payload, \"references\", content_hash) VALUES (:id, 'NORMAL', 'SECURITY', :subject, :profile, 1, 't', 'v3', :now, :now, :sn, :fr, 5000, 1, 1, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, :h)",
                {
                    "id": context_id,
                    "subject": str(security_id),
                    "profile": profile_id,
                    "now": now,
                    "sn": snapshot_id,
                    "fr": feature_run_id,
                    "h": context_hash,
                },
            ),
            (
                "INSERT INTO v3.task_runs (task_run_id, task_profile_id, task_profile_version, status, expected_group_count, successful_group_count, failed_group_count, pending_group_count, context_pack_id, context_pack_hash) VALUES (:id, :profile, 1, 'PENDING_IMPORT', 1, 0, 0, 1, :ctx, :h)",
                {
                    "id": task_run_id,
                    "profile": profile_id,
                    "ctx": context_id,
                    "h": context_hash,
                },
            ),
            (
                "INSERT INTO v3.agent_tasks (task_id, task_run_id, task_type, subject, task_profile, trigger_type, as_of, context_pack_id, context_pack_hash, expected_result_type, constraints, content_hash) VALUES (:id, :run, 'AI_PROPOSAL', '{}'::jsonb, 'NORMAL', 'SCHEDULED', :now, :ctx, :ch, :ert, '{}'::jsonb, :th)",
                {
                    "id": task_id,
                    "run": task_run_id,
                    "now": now,
                    "ctx": context_id,
                    "ch": context_hash,
                    "ert": expected_result_type,
                    "th": unique_hash(),
                },
            ),
        ]
        for statement, params in statements:
            await connection.execute(text(statement), params)
    return {
        "security_id": security_id,
        "task_id": task_id,
        "task_run_id": task_run_id,
        "context_id": context_id,
        "context_hash": context_hash,
    }


def _envelope(fixture: dict, result: dict, result_type: str) -> AIResultEnvelope:
    return AIResultEnvelope.build(
        {
            "result_id": uuid4(),
            "result_type": result_type,
            "agent": AgentIdentity(
                agent_type=AgentType.CHATGPT_WEB,
                provider=AgentProvider.OPENAI,
                model="rt09",
            ),
            "task_id": fixture["task_id"],
            "task_run_id": fixture["task_run_id"],
            "task_profile": "NORMAL",
            "trigger_type": "SCHEDULED",
            "context_pack_id": fixture["context_id"],
            "context_pack_hash": fixture["context_hash"],
            "prompt_version": "rt09",
            "strategy_version": "v3",
            "produced_at": fixture_now,
            "as_of": fixture_now,
            "result": result,
        }
    )


async def _insert_envelope(engine, envelope: AIResultEnvelope) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v3.ai_result_envelopes "
                "(result_id, task_id, task_run_id, schema_version, result_type, agent_type, "
                "provider, model, context_pack_id, context_pack_hash, prompt_version, "
                "strategy_version, produced_at, as_of, known_at, evidence_ids, payload, "
                "content_hash) VALUES (:id, :task, :run, 'v3.0', :rtype, "
                "'CHATGPT_WEB', 'OPENAI', 'rt09', :ctx, :ch, 'rt09', 'v3', "
                ":now, :now, :now, '[]'::jsonb, :payload, :hash)"
            ),
            {
                "id": envelope.result_id,
                "task": envelope.task_id,
                "run": envelope.task_run_id,
                "rtype": envelope.result_type,
                "ctx": envelope.context_pack_id,
                "ch": envelope.context_pack_hash,
                "now": fixture_now,
                "payload": json.dumps(envelope.model_dump(mode="json")),
                "hash": envelope.content_hash,
            },
        )


def _decision_result(fixture: dict) -> dict:
    return {
        "security_id": str(fixture["security_id"]),
        "direction": "LONG",
        "reason": "rt09 typed schema closure",
    }


def _entry_plan_result(fixture: dict, decision_id, plan: dict) -> dict:
    return {
        "decision_id": str(decision_id),
        "version": 1,
        "expected_horizon": "SWING",
        "plan": plan,
    }


VALID_PLAN = {
    "entry_mode": "LIMIT_PULLBACK",
    "entry_zone": {"low": 9.5, "high": 10.0},
    "triggers": [{"kind": "PRICE_ABOVE", "value": 10.05}],
    "cancels": [{"kind": "PRICE_BELOW", "value": 9.2}],
    "stop": {"price": 9.0, "reason": "invalidation"},
    "targets": [{"price": 12.0, "target_type": "T1"}],
    "max_wait_sessions": 3,
}

INVALID_PLAN = {
    "entry_mode": "LIMIT_PULLBACK",
    # PRICE_ABOVE 触发条件缺 value：客观条件不完整，必须拒绝
    "triggers": [{"kind": "PRICE_ABOVE"}],
}


@pytest.mark.asyncio
async def test_entry_plan_import_rejects_invalid_typed_schema() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _setup(engine, "DecisionResult")
    decision_envelope = _envelope(fixture, _decision_result(fixture), "DecisionResult")
    await _insert_envelope(engine, decision_envelope)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        created = await uow.ai_imports._materialize(decision_envelope)
        await uow.commit()
    decision_id = created[0]

    plan_fixture = await _setup(engine, "EntryPlanResult")
    plan_envelope = _envelope(
        plan_fixture,
        _entry_plan_result(plan_fixture, decision_id, INVALID_PLAN),
        "EntryPlanResult",
    )
    await _insert_envelope(engine, plan_envelope)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        with pytest.raises(RepositoryConflictError):
            await uow.ai_imports._materialize(plan_envelope)
    await engine.dispose()


@pytest.mark.asyncio
async def test_entry_plan_import_accepts_typed_schema_and_never_creates_trade() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _setup(engine, "DecisionResult")
    decision_envelope = _envelope(fixture, _decision_result(fixture), "DecisionResult")
    await _insert_envelope(engine, decision_envelope)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        created = await uow.ai_imports._materialize(decision_envelope)
        await uow.commit()
    decision_id = created[0]

    plan_fixture = await _setup(engine, "EntryPlanResult")
    plan_envelope = _envelope(
        plan_fixture,
        _entry_plan_result(plan_fixture, decision_id, VALID_PLAN),
        "EntryPlanResult",
    )
    await _insert_envelope(engine, plan_envelope)
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        created = await uow.ai_imports._materialize(plan_envelope)
        await uow.commit()
    assert len(created) == 1

    async with engine.connect() as connection:
        trade_count = await connection.scalar(
            text(
                "SELECT count(*) FROM v3.trade_ledger WHERE security_id IN (:s1, :s2)"
            ),
            {"s1": fixture["security_id"], "s2": plan_fixture["security_id"]},
        )
    await engine.dispose()
    # 不能写真实 Trade：AI Result 导入全链路 0 行 trade_ledger
    assert trade_count == 0
    # _materialize 直连不走 preview/confirm，不要求审计行


@pytest.mark.asyncio
async def test_preview_confirm_writes_audit_and_is_idempotent() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _setup(engine, "DecisionResult")
    decision_envelope = _envelope(fixture, _decision_result(fixture), "DecisionResult")
    bundle = AIResultBundle.from_single(decision_envelope)

    preview = await PreviewAIResultImportService(
        lambda: SQLAlchemyUnitOfWork(sessions),
    ).execute(bundle)
    assert preview.groups[0].valid

    command = AIResultConfirmCommand(
        preview_revision=preview.preview_revision,
        bundle_hash=bundle.bundle_hash,
        idempotency_key=f"rt09-confirm-{uuid4().hex * 2}",
        confirmed_by="human",
    )
    confirm_service = ConfirmAIResultImportService(
        lambda: SQLAlchemyUnitOfWork(sessions),
    )
    first = await confirm_service.execute(preview.import_id, command)
    assert first.status.value in ("CONFIRMED", "PARTIAL_COMPLETED")

    # Idempotency：同一 import 重复 confirm，不重复落库、不报错
    second = await confirm_service.execute(preview.import_id, command)

    async with engine.connect() as connection:
        audit_rows = (
            await connection.execute(
                text(
                    "SELECT action, result FROM v3.audit_events "
                    "WHERE object_type='ai_result_import' AND object_id=:oid "
                    "ORDER BY event_time",
                ),
                {"oid": str(preview.import_id)},
            )
        ).all()
        decision_count = await connection.scalar(
            text("SELECT count(*) FROM v3.decisions WHERE source_result_id=:rid"),
            {"rid": decision_envelope.result_id},
        )
    await engine.dispose()
    assert len(first.successful_groups) == 1
    assert len(second.successful_groups) == 1  # 短路复用，不重复创建
    assert decision_count == 1
    actions = {row.action for row in audit_rows}
    assert "AI_RESULT_PREVIEW" in actions
    assert "AI_RESULT_CONFIRM" in actions
    assert all(row.result == "SUCCESS" for row in audit_rows)
