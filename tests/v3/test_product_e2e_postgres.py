"""RC-10 产品级完整 E2E（TEST-001）。

方案 §13.3 场景：Universe → Bars → Feature/Regime → Evidence → Recall →
Comparison → Context → AI Decision/Entry Plan → Trade Draft → 人工 Confirm →
Position Rebuild → Position Context → Position Review → 未来 Bars →
Performance Mature → 历史重放 → Shadow → Audit → 幂等重跑无重复事实。

失败场景（provider failure / oversell race / correction chain / opening
boundary / unauthenticated WRITE 等）由各域专项测试覆盖（见实施记录 RC-10）。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.build_candidate_comparison import (
    BuildCandidateComparisonService,
    CandidateComparisonQuery,
)
from app.v3.application.build_context_pack import (
    BuildContextPackCommand,
    BuildContextPackService,
)
from app.v3.application.import_ai_results import (
    ConfirmAIResultImportService,
    PreviewAIResultImportService,
)
from app.v3.application.ingest_evidence import IngestEvidenceBatchService
from app.v3.application.mature_performance import MaturePerformanceService
from app.v3.application.mature_recall_observations import (
    MatureRecallObservationsService,
    RecallMissThreshold,
)
from app.v3.application.run_full_market_features import RunFullMarketFeaturesService
from app.v3.application.run_multi_recall import RunMultiRecallService
from app.v3.application.deterministic_replay import DeterministicReplayService
from app.v3.application.shadow_executor import ShadowExecutorService
from app.v3.domain.evidence import EvidenceType
from app.v3.contracts.agent import (
    AgentIdentity,
    AgentProvider,
    AgentType,
    AIResultEnvelope,
)
from app.v3.domain.ai_import import (
    AIResultAtomicGroup,
    AIResultBundle,
    AIResultConfirmCommand,
    GroupCommitStatus,
)
from app.v3.domain.context import ContextLevel, ContextSubjectType
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    PointInTimePrecision,
    Market,
    MarketBar,
    SecurityMember,
    UniverseSnapshot,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)
from app.v3.application.manage_portfolio import PortfolioWriteService
from app.v3.domain.performance import ReplayRunCreate
from app.v3.domain.portfolio import (
    AccountCreate,
    TradeConfirm,
    TradeDraftCreate,
    TradeSide,
)
from app.v3.domain.strategy import (
    ActorType,
    ExperimentEventCommand,
    GuardrailVersionCreate,
    StrategyExperimentCreate,
    StrategyVersionCreate,
)
from app.v3.domain.evidence import (
    DecayModel,
    EvidenceSourceType,
    EvidenceSource,
    FetchedDocument,
    NormalizedEvidence,
    RawDocument,
)
from app.v3.domain.recall import RecallChannel
from app.v3.infrastructure.db.models import TaskProfileModel
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.providers.calendar import TradingCalendarMetadata
from app.v3.providers.evidence import EvidenceFetchBatch, ParsedEvidenceBundle
from app.v3.providers.recall import (
    ChannelEvaluation,
    ObservationOutcome,
    RecallCandidate,
)

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
CONTEXT_AT = NOW + timedelta(minutes=5)
LATER = datetime(2026, 9, 21, 8, tzinfo=timezone.utc)


class Calendar:
    metadata = TradingCalendarMetadata(
        source="fixture", source_version="v1", calendar_code="XSHG",
        coverage_start=date(2020, 1, 1), coverage_end=date(2030, 1, 1),
    )

    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5


class AllChannel:
    channel = RecallChannel.build(
        code="E2E_ALL", version="v1", configuration={"fixture": True},
        description="E2E 全量通道",
    )

    def evaluate(self, features, _evidence):
        return ChannelEvaluation(
            evaluated_count=len(features), unavailable_count=0,
            candidates=tuple(RecallCandidate(
                security_id=item.security_id, strength=0.5,
                reasons=("e2e",), matched_features={"e2e": True},
                coverage=item.coverage,
            ) for item in features),
        )


class FixedOutcomeProvider:
    async def resolve(self, observations, *, as_of):
        return tuple(ObservationOutcome(
            pending_observation_id=item.observation_id,
            future_price=round(item.baseline_price * 1.2, 6),
            benchmark_return=0.05,
        ) for item in observations)


def _revision(security_id, *, end: datetime, days: int = 260) -> BarSeriesRevision:
    bars = tuple(
        MarketBar(
            bar_time=end - timedelta(days=days - index),
            open=10 + index / 100, high=10.5 + index / 100,
            low=9.5 + index / 100, close=10.2 + index / 100,
            volume=1000 + index, amount=2_000_000 + index * 1000,
            fetch_time=end - timedelta(minutes=1),
        )
        for index in range(days)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=security_id, period=BarPeriod.DAY,
        adjust_type=AdjustType.QFQ, source="e2e-fixture", upstream_source="e2e-fixture",
        raw_bar_available=False, point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="e2e QFQ only", known_at=end - timedelta(seconds=1), bars=bars,
    ))


def _evidence_record(raw: RawDocument, source: EvidenceSource) -> NormalizedEvidence:
    return NormalizedEvidence.build(
        raw_document_id=raw.raw_document_id,
        evidence_type=EvidenceType.OFFICIAL_DISCLOSURE,
        source_type=source.source_type,
        source_priority=source.priority,
        subject_type=ContextSubjectType.SECURITY,
        subject_id="SH:600011",
        claim_key="disclosure:e2e-1",
        source=source.code,
        upstream_source=source.upstream_source,
        payload={"title": "e2e disclosure"},
        normalized_payload={"title": "e2e disclosure", "category": "ANNOUNCEMENT"},
        event_time=NOW - timedelta(hours=2),
        publish_time=NOW - timedelta(hours=1),
        fetch_time=NOW,
        known_at=NOW,
        confidence=1,
        relevance=0.9,
        decay_model=DecayModel.NONE,
        parser_version="e2e-v1",
    )


class E2EProvider:
    def __init__(self, source: EvidenceSource):
        self.source = source
        self.documents = (FetchedDocument(
            document_key="e2e-1",
            raw_reference="https://example.invalid/disclosure/e2e-1?b=2&a=1",
            mime_type="application/json",
            payload_text='{"title":"e2e disclosure"}',
            fetch_time=NOW, known_at=NOW,
        ),)

    async def fetch(self, **_kwargs) -> EvidenceFetchBatch:
        return EvidenceFetchBatch(documents=self.documents, exhausted=True)

    async def close(self) -> None:
        return None


class E2EParser:
    code = "e2e"
    version = "e2e-v1"

    def parse(self, raw: RawDocument, source: EvidenceSource) -> ParsedEvidenceBundle:
        return ParsedEvidenceBundle(records=(_evidence_record(raw, source),), links=())


def _envelope(result_type: str, result: dict, *, task_id, task_run_id,
              context_pack_id, context_hash: str, as_of: datetime) -> AIResultEnvelope:
    return AIResultEnvelope.build({
        "result_id": uuid4(), "result_type": result_type,
        "agent": AgentIdentity(agent_type=AgentType.CHATGPT_WEB,
                               provider=AgentProvider.OPENAI, model="e2e"),
        "task_id": task_id, "task_run_id": task_run_id,
        "task_profile": "E2E", "trigger_type": "USER_REQUEST",
        "context_pack_id": context_pack_id, "context_pack_hash": context_hash,
        "prompt_version": "e2e", "strategy_version": "v3",
        "produced_at": as_of, "as_of": as_of, "result": result,
    })


@pytest.mark.asyncio
async def test_product_e2e_full_pipeline_to_maturity_and_replay() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    # 0. 任务档案（context pack 外键要求）
    profile_id = uuid4()
    async with sessions() as session:
        session.add(TaskProfileModel(
            task_profile_id=profile_id, profile_code=f"e2e-{uuid4().hex[:8]}",
            version=1, timezone="Asia/Shanghai", context_level="NORMAL",
            output_schema={}, content_hash=uuid4().hex + uuid4().hex,
        ))
        await session.commit()

    # 1. 发布 Universe
    snapshot = UniverseSnapshot.build(UniverseSnapshotContent(
        snapshot_id=uuid4(), source_code=f"e2e-{uuid4().hex}",
        status=UniverseSnapshotStatus.PRIMARY, as_of=NOW - timedelta(minutes=2),
        fetch_time=NOW - timedelta(minutes=2), known_at=NOW - timedelta(minutes=2),
        coverage=1.0, stale=False,
        members=tuple(
            SecurityMember(code=f"{600000 + index:06d}", market=Market.SH,
                           name=f"e2e-{index}")
            for index in range(20)
        ),
    ))
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.universes.publish(snapshot) is True
        await uow.commit()

    # 2. 发布历史 Bars
    first_revision = _revision(uuid4(), end=NOW)  # 占位，稍后被真实值替换
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        targets = await uow.universes.targets(snapshot.snapshot_id)
        revisions = [_revision(item.security_id, end=NOW) for item in targets]
        first_revision = revisions[0]
        security_ids = [item.security_id for item in targets]
        for revision in revisions:
            assert await uow.bars.publish_series_revision(revision) is True
        await uow.commit()

    # 3+4. Feature Run + Market Regime（服务内一并发布）
    run = await RunFullMarketFeaturesService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW
    ).execute(universe_snapshot_id=snapshot.snapshot_id, as_of=NOW)
    assert run.coverage == 1.0

    # 5. Evidence 摄取
    source = EvidenceSource(
        code=f"e2e-official-{uuid4().hex}",
        source_type=EvidenceSourceType.OFFICIAL,
        upstream_source="e2e-exchange",
        capabilities={"types": ["OFFICIAL_DISCLOSURE"]},
        priority=1, parser_version="e2e-v1", reliability=1,
    )
    ingest = await IngestEvidenceBatchService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW
    ).execute(provider=E2EProvider(source), parser=E2EParser())
    assert ingest.evidence_count == 1 and ingest.failed_count == 0

    # 6. Recall（重放同 run）
    recall_service = RunMultiRecallService(
        lambda: SQLAlchemyUnitOfWork(sessions), Calendar(),
        channels=(AllChannel(),), clock=lambda: NOW + timedelta(minutes=1),
    )
    recall = await recall_service.execute(feature_run_id=run.feature_run_id)
    replay_recall = await recall_service.execute(feature_run_id=run.feature_run_id)
    assert replay_recall.recall_run_id == recall.recall_run_id  # 幂等

    # 7. Candidate Comparison
    comparison = await BuildCandidateComparisonService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: NOW
    ).execute(CandidateComparisonQuery(
        codes=tuple(f"{600000 + index:06d}" for index in range(20)),
        feature_run_id=run.feature_run_id, as_of=NOW))
    assert comparison.comparison_pack_id is not None

    # 8. Security Context Pack
    context = await BuildContextPackService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: CONTEXT_AT
    ).execute(BuildContextPackCommand(
        context_level=ContextLevel.NORMAL, subject_type=ContextSubjectType.SECURITY,
        subject_id="SH:600011", task_profile_id=profile_id,
        task_profile_version=1, as_of=CONTEXT_AT, feature_run_id=run.feature_run_id,
        recall_run_id=recall.recall_run_id,
        comparison_pack_id=comparison.comparison_pack_id,
    ))
    assert context.context_pack_id is not None

    # 9. AI Decision + Entry Plan 导入（注册 task_run/agent_task → preview → confirm）
    task_run_id = uuid4()
    decision_id = uuid4()
    entry_plan_id = uuid4()
    decision_task_id = uuid4()
    plan_task_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO v3.task_runs (task_run_id, task_profile_id, task_profile_version, "
            "status, expected_group_count, successful_group_count, failed_group_count, "
            "pending_group_count, context_pack_id, context_pack_hash) VALUES "
            "(:id, :profile, 1, 'PENDING_IMPORT', 1, 0, 0, 1, :context, :hash)"
        ), {"id": task_run_id, "profile": profile_id,
            "context": context.context_pack_id, "hash": context.content_hash})
        for task_id, result_type in ((decision_task_id, "DecisionResult"),
                                     (plan_task_id, "EntryPlanResult")):
            await connection.execute(text(
                "INSERT INTO v3.agent_tasks (task_id, task_run_id, task_type, subject, "
                "task_profile, trigger_type, as_of, context_pack_id, context_pack_hash, "
                "expected_result_type, constraints, content_hash) VALUES "
                "(:id, :run, 'STOCK_REVIEW', '{}'::jsonb, 'E2E', 'USER_REQUEST', :now, "
                ":context, :hash, :result_type, '{}'::jsonb, :task_hash)"
            ), {"id": task_id, "run": task_run_id, "now": CONTEXT_AT,
                "context": context.context_pack_id, "hash": context.content_hash,
                "result_type": result_type, "task_hash": uuid4().hex + uuid4().hex})

    bundle = AIResultBundle.build(
        agent=AgentIdentity(agent_type=AgentType.CHATGPT_WEB,
                            provider=AgentProvider.OPENAI, model="e2e"),
        task_run_ids=(task_run_id,),
        produced_at=NOW,
        atomic_groups=(AIResultAtomicGroup.build(
            group_id="e2e-group-1", task_run_id=task_run_id,
            results=(
                _envelope("DecisionResult",
                          {"decision_id": str(decision_id),
                           "security_id": str(security_ids[0]),
                           "decision": "OBSERVE", "direction": "LONG",
                           "strategy_version": "e2e-v1",
                           "original_entry_plan_id": str(entry_plan_id),
                           "original_entry_plan": {
                               "entry_plan_id": str(entry_plan_id),
                               "plan": {"take_profit": 12.0, "stop_loss": 9.0,
                                        "expected_horizon": "SWING"},
                           }},
                          task_id=decision_task_id, task_run_id=task_run_id,
                          context_pack_id=context.context_pack_id,
                          context_hash=context.content_hash, as_of=CONTEXT_AT),
                _envelope("EntryPlanResult",
                          {"entry_plan_id": str(entry_plan_id),
                           "decision_id": str(decision_id),
                           "version": 1, "plan": {"horizon": "SWING"}},
                          task_id=plan_task_id, task_run_id=task_run_id,
                          context_pack_id=context.context_pack_id,
                          context_hash=context.content_hash, as_of=CONTEXT_AT),
            ),
            dependencies={},
        ),),
    )
    import_preview = await PreviewAIResultImportService(
        lambda: SQLAlchemyUnitOfWork(sessions)
    ).execute(bundle)
    assert all(group.valid for group in import_preview.groups), import_preview
    confirm = await ConfirmAIResultImportService(
        lambda: SQLAlchemyUnitOfWork(sessions)
    ).execute(import_preview.import_id, AIResultConfirmCommand(
        preview_revision=import_preview.preview_revision,
        bundle_hash=import_preview.bundle.bundle_hash,
        idempotency_key=f"e2e-import-{uuid4().hex * 2}",
        confirmed_by="ops-e2e",
    ))
    assert all(group.status == GroupCommitStatus.COMMITTED
               for group in confirm.successful_groups), confirm
    assert not confirm.failed_groups, confirm

    # 10+11. Trade Draft → 人工确认
    account_id = uuid4()
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        buy_security_id = security_ids[0]
        await uow.portfolios.add_account(AccountCreate(
            account_id=account_id, name=f"e2e-acct-{uuid4().hex[:8]}",
        ))
        draft_id = await uow.portfolios.add_trade_draft(TradeDraftCreate(
            account_id=account_id, security_id=buy_security_id,
            side=TradeSide.BUY, trade_time=CONTEXT_AT, price=Decimal("10.20"),
            quantity=Decimal("1000"), fee=Decimal("5"),
        ))
        await uow.commit()
    trade = await PortfolioWriteService(
        lambda: SQLAlchemyUnitOfWork(sessions)
    ).confirm_trade(draft_id, TradeConfirm(
        idempotency_key=f"e2e-confirm-{uuid4().hex}", confirmed_by="ops-e2e",
    ))
    assert trade["status"] == "CONFIRMED"

    # 12. Position Projection 重建
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position = await uow.portfolios.position(account_id, buy_security_id)
    assert position is not None and position["quantity"] == Decimal("1000")

    # 13. Position Context Pack
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        position_source = await uow.context_packs.load_source(
            subject_type=ContextSubjectType.POSITION.value,
            subject_id=f"{account_id}:SH:600000",
            as_of=CONTEXT_AT, feature_run_id=run.feature_run_id,
        )
    assert position_source is not None
    assert position_source.portfolio is not None
    assert position_source.portfolio.quantity == Decimal("1000")

    # 14. Position Review 导入
    review_task_id = uuid4()
    review_run_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO v3.task_runs (task_run_id, task_profile_id, task_profile_version, "
            "status, expected_group_count, successful_group_count, failed_group_count, "
            "pending_group_count, context_pack_id, context_pack_hash) VALUES "
            "(:id, :profile, 1, 'PENDING_IMPORT', 1, 0, 0, 1, :context, :hash)"
        ), {"id": review_run_id, "profile": profile_id,
            "context": context.context_pack_id, "hash": context.content_hash})
        await connection.execute(text(
            "INSERT INTO v3.agent_tasks (task_id, task_run_id, task_type, subject, "
            "task_profile, trigger_type, as_of, context_pack_id, context_pack_hash, "
            "expected_result_type, constraints, content_hash) VALUES "
            "(:id, :run, 'POSITION_REVIEW', '{}'::jsonb, 'E2E', 'USER_REQUEST', :now, "
            ":context, :hash, 'PositionReviewResult', '{}'::jsonb, :task_hash)"
        ), {"id": review_task_id, "run": review_run_id, "now": CONTEXT_AT,
            "context": context.context_pack_id, "hash": context.content_hash,
            "task_hash": uuid4().hex + uuid4().hex})
    review_bundle = AIResultBundle.build(
        agent=AgentIdentity(agent_type=AgentType.CHATGPT_WEB,
                            provider=AgentProvider.OPENAI, model="e2e"),
        task_run_ids=(review_run_id,), produced_at=CONTEXT_AT,
        atomic_groups=(AIResultAtomicGroup.build(
            group_id="e2e-review", task_run_id=review_run_id,
            results=(_envelope("PositionReviewResult",
                               {"account_id": str(account_id),
                                "security_id": str(buy_security_id),
                                "position_projection_hash": position["input_hash"],
                                "recommended_action": "HOLD", "reason": "e2e review"},
                               task_id=review_task_id, task_run_id=review_run_id,
                               context_pack_id=context.context_pack_id,
                               context_hash=context.content_hash, as_of=CONTEXT_AT),),
            dependencies={},
        ),),
    )
    review_preview = await PreviewAIResultImportService(
        lambda: SQLAlchemyUnitOfWork(sessions)
    ).execute(review_bundle)
    assert all(group.valid for group in review_preview.groups), review_preview
    review_confirm = await ConfirmAIResultImportService(
        lambda: SQLAlchemyUnitOfWork(sessions)
    ).execute(review_preview.import_id, AIResultConfirmCommand(
        preview_revision=review_preview.preview_revision,
        bundle_hash=review_preview.bundle.bundle_hash,
        idempotency_key=f"e2e-review-{uuid4().hex * 2}",
        confirmed_by="ops-e2e",
    ))
    assert all(group.status == GroupCommitStatus.COMMITTED
               for group in review_confirm.successful_groups), review_confirm
    assert not review_confirm.failed_groups, review_confirm

    # 15. 未来 Bars
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        assert await uow.bars.publish_series_revision(
            _revision(buy_security_id, end=LATER)
        ) is True
        await uow.commit()

    # 16. Performance Mature（观察成熟 + 指标生成）
    maturity = await MatureRecallObservationsService(
        lambda: SQLAlchemyUnitOfWork(sessions), FixedOutcomeProvider(),
        threshold=RecallMissThreshold(version="e2e-v1", raw_return_gte=0.15),
        clock=lambda: LATER,
    ).execute()
    assert maturity.matured_count > 0
    perf = await MaturePerformanceService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: LATER
    ).execute(as_of=LATER)
    assert perf["matured_count"] > 0

    # 17. 历史重放（同 as_of 冻结输入）
    replay = await DeterministicReplayService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: LATER
    ).execute(ReplayRunCreate(
        strategy_version="e2e-replay", replay_as_of=NOW,
        bar_revision_ids=(first_revision.revision_id,),
        context_pack_ids=(context.context_pack_id,),
    ))
    assert replay["replay_run_id"] is not None

    # 18. Shadow run
    control_id = treatment_id = None
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        guardrail_id = await uow.strategies.add_guardrail(GuardrailVersionCreate(
            guardrail_code=f"gr-{uuid4().hex[:8]}", version=1,
            max_error_rate=Decimal("0.05"), max_p95_ms=Decimal("2000"),
            min_shadow_sample_count=1, max_divergence_rate=Decimal("0.10"),
            max_capacity_utilization=Decimal("0.8"), created_by="e2e",
        ))
        await uow._session.flush()

        async def _version():
            version_id = uuid4()
            await uow.strategies.add_strategy_version(StrategyVersionCreate(
                strategy_version_id=version_id,
                strategy_code=f"strat-{uuid4().hex[:8]}", version=1,
                configuration={"horizons": [1, 3, 5]}, rationale="e2e",
                created_by="e2e",
            ))
            await uow._session.flush()
            return version_id

        control_id = await _version()
        treatment_id = await _version()
        await uow.commit()
    experiment_id = uuid4()
    # 实验 + 事件走产品服务路径（同事务写审计）
    from app.v3.application.manage_strategy import StrategyStabilizationService
    strategy_service = StrategyStabilizationService(
        lambda: SQLAlchemyUnitOfWork(sessions)
    )
    await strategy_service.add_experiment(StrategyExperimentCreate(
        experiment_id=experiment_id, experiment_type="SHADOW",
        control_strategy_version_id=control_id,
        treatment_strategy_version_id=treatment_id,
        guardrail_version_id=guardrail_id, allocation_percent=0,
        starts_at=NOW, created_by="e2e",
    ))
    await strategy_service.experiment_event(experiment_id, ExperimentEventCommand(
        event_type="STARTED", actor_type=ActorType.HUMAN,
        actor_id="ops-e2e", reason="e2e shadow",
    ))

    async def _control(subject_key, as_of):
        return {"rank": 1}

    async def _treatment(subject_key, as_of):
        return {"rank": 2}

    shadow = await ShadowExecutorService(
        lambda: SQLAlchemyUnitOfWork(sessions),
        executors={control_id: _control, treatment_id: _treatment},
        clock=lambda: LATER,
    ).execute(experiment_id, "SH:600000")
    assert shadow["materially_divergent"] is True

    # 19. Audit 核验
    async with engine.connect() as connection:
        audit_count = await connection.scalar(text(
            "SELECT count(*) FROM v3.audit_events WHERE object_id = :tid "
            "AND action = 'TRADE_CONFIRMED'"
        ), {"tid": str(trade["trade_id"])})
        experiment_audit = await connection.scalar(text(
            "SELECT count(*) FROM v3.audit_events WHERE object_id = :eid"
        ), {"eid": str(experiment_id)})
    assert audit_count >= 1
    assert experiment_audit >= 1

    # 20. 幂等重跑：Evidence 去重 / Recall 与 Comparison 内容重放
    rerun_ingest = await IngestEvidenceBatchService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: LATER
    ).execute(provider=E2EProvider(source), parser=E2EParser())
    assert rerun_ingest.duplicate_count == 1 and rerun_ingest.evidence_count == 0
    rerun_recall = await recall_service.execute(feature_run_id=run.feature_run_id)
    assert rerun_recall.recall_run_id == recall.recall_run_id
    rerun_comparison = await BuildCandidateComparisonService(
        lambda: SQLAlchemyUnitOfWork(sessions), clock=lambda: LATER
    ).execute(CandidateComparisonQuery(
        codes=tuple(f"{600000 + index:06d}" for index in range(20)),
        feature_run_id=run.feature_run_id, as_of=NOW))
    assert rerun_comparison.comparison_pack_id == comparison.comparison_pack_id

    await engine.dispose()

    await engine.dispose()
