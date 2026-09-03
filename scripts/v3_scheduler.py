"""V3 正式生产调度器（RC-03 / OPS-001）。

统一 Orchestrator：
- 收盘后主链（交易日）：Universe/日线增量/公司行动摄取（Phase2 market job）
  → 指数基准（东财失败逐基准降级腾讯，RT §23.1）→ 全市场 Feature Run + Market Regime
  → Evidence 增量（24h 窗口，RT §7.2 Step 09）
  → Full Recall + Raw Opportunity Publish（RT §7.2 Step 10/11，
    RawOpp 由 RunMultiRecallService.publish 一并落库）；
- 独立每日维护链：Corporate Action Match、Projection Verify。

每个 Job 的运行记录（status/as_of/known_at/attempt/error/metrics）由
Orchestrator 落库到 v3.orchestrator_job_runs；按交易日幂等，重复执行
自动跳过；全局 advisory lock 防止并发重复调度。
Evidence 部分能力失败不阻断（仅全部失败才 FAILED），Recall 通道自带
失败声明（failed_channel_count 记录），与生产诚实原则一致。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID

from app.config import Settings
from app.providers.tencent import TencentProvider
from app.services.data_quality import DataQualityService
from app.utils.time import SHANGHAI
from app.v3.application.attention_engine import AttentionEngineService
from app.v3.application.intraday_market_data import IntradayMarketDataService
from app.v3.jobs.intraday_loop import IntradayTriggerLoop
from app.v3.application.evaluate_evidence_recall_channels import (
    evidence_recall_channels,
)
from app.v3.application.evaluate_recall_channels import feature_recall_channels
from app.v3.application.ingest_evidence import IngestEvidenceBatchService
from app.v3.application.ingest_index_benchmarks import IngestIndexBenchmarksService
from app.v3.application.link_evidence_entities import EvidenceEntityMatcher
from app.v3.application.mature_performance import MaturePerformanceService
from app.v3.application.mature_recall_observations import (
    MatureRecallObservationsService,
    RecallMissThreshold,
)
from app.v3.application.match_corporate_actions import MatchCorporateActionsService
from app.v3.application.release_resolver import ReleaseResolver
from app.v3.application.run_evidence_ingestion import RunEvidenceIngestionService
from app.v3.application.run_evidence_registry import (
    CapabilityRunStatus,
    RunEvidenceRegistryService,
)
from app.v3.application.run_full_market_features import RunFullMarketFeaturesService
from app.v3.application.run_multi_recall import RunMultiRecallService
from app.v3.application.verify_position_projections import (
    VerifyPositionProjectionsService,
)
from app.v3.infrastructure.db.session import V3Database
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.providers.eastmoney import EastmoneyProvider
from app.v3.infrastructure.providers.exchange_calendar import (
    ExchangeCalendarsAShareCalendar,
)
from app.v3.providers.bars_outcome import BarsOutcomeProvider
from app.v3.providers.evidence import EvidenceCapability
from app.v3.jobs.market_data import catchup_trade_dates, latest_completed_session
from app.v3.jobs.orchestrator import JobDefinition, Orchestrator
from scripts.v3_phase2_market_job import build_parser as build_job_parser
from scripts.v3_phase2_market_job import execute as execute_market_job
from scripts.v3_phase4_evidence import build_registry as build_evidence_registry


CORPORATE_ACTION_LOOKBACK_DAYS = 10
EVIDENCE_WINDOW = timedelta(days=1)


def _evidence_failed_capabilities(report) -> list[str]:
    return [
        item.capability.value for item in report.capabilities
        if item.status in {CapabilityRunStatus.FAILED, CapabilityRunStatus.UNAVAILABLE}
    ]


async def _resolve_feature_run_id(context) -> str:
    """优先同一次编排里 features Job 的产物，追平时退回最新 PUBLISHED run。"""
    artifact = context.artifact("features")
    if artifact.get("feature_run_id"):
        return artifact["feature_run_id"]
    async with context.uow_factory() as uow:
        latest = await uow.features.latest_run()
    if latest is None:
        raise RuntimeError("no published feature run available for recall")
    return str(latest.feature_run_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Schedule the V3 production pipeline")
    parser.add_argument(
        "--at", default=os.getenv("V3_SCHEDULE_AT", "18:45"), type=_schedule_time,
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--intraday-once", action="store_true",
        help="跑一轮盘中触发评估后退出（部署烟测用，不进入任何循环）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.getenv("V3_SCHEDULE_REPORT_PATH", "/tmp/v3-scheduler-last.json")),
    )
    return parser


def _schedule_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "schedule time must be HH:MM or HH:MM:SS"
        ) from exc
    if parsed.tzinfo is not None:
        raise argparse.ArgumentTypeError("schedule time must not include a timezone")
    return parsed


def seconds_until_next_run(now: datetime, scheduled: datetime.time) -> float:
    local = now.astimezone(SHANGHAI)
    target = datetime.combine(local.date(), scheduled, tzinfo=SHANGHAI)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


def build_orchestrators(database_url: str) -> tuple[Orchestrator, Orchestrator, V3Database]:
    settings = Settings(_env_file=None, v3_database_url=database_url)
    database = V3Database(
        database_url,
        echo=settings.v3_database_echo,
        pool_size=settings.v3_database_pool_size,
        max_overflow=settings.v3_database_max_overflow,
    )

    def uow_factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(database.sessions)

    eastmoney = EastmoneyProvider(settings)

    async def market_data_handler(context) -> dict:
        report = await execute_market_job(
            build_job_parser().parse_args(["--mode", "all"])
        )
        return {
            "status": report.get("status"),
            "universe": {
                key: report.get("universe", {}).get(key)
                for key in ("snapshot_id", "status", "coverage", "stale")
            },
            "backfill": {
                key: report.get("backfill", {}).get(key)
                for key in ("status", "processed_count", "successful_count", "failed_count")
            },
            "corporate_actions": {
                key: report.get("corporate_actions", {}).get(key)
                for key in ("published_count", "unchanged_count", "provider_errors")
            },
        }

    async def features_handler(context) -> dict:
        async with context.uow_factory() as uow:
            snapshot = await uow.universes.latest()
        if snapshot is None:
            raise RuntimeError("no universe snapshot available for feature run")
        service = RunFullMarketFeaturesService(context.uow_factory)
        run = await service.execute(
            universe_snapshot_id=snapshot.snapshot_id, as_of=context.as_of
        )
        return {
            "feature_run_id": str(run.feature_run_id),
            "status": run.status.value,
            "expected_count": run.expected_count,
            "successful_count": run.successful_count,
            "failed_count": run.failed_count,
            "coverage": str(run.coverage),
        }

    async def evidence_increment_handler(context) -> dict:
        """Evidence 增量（RT §7.2 Step 09）：24h 窗口，复用 phase4 注册表。

        失败策略与 index-benchmarks 一致：仅全部能力失败才 FAILED，
        部分能力失败如实记入 failed_capabilities，不阻断下游 Recall
        （Recall 通道自带可用性声明）。
        """
        async with context.uow_factory() as uow:
            snapshot = await uow.universes.latest()
        if snapshot is None:
            raise RuntimeError("no universe snapshot available for evidence ingestion")
        registry = build_evidence_registry(tuple(member.code for member in snapshot.members))
        try:
            batch_service = IngestEvidenceBatchService(
                context.uow_factory,
                entity_linker=EvidenceEntityMatcher.from_universe(snapshot),
            )
            service = RunEvidenceRegistryService(
                registry,
                RunEvidenceIngestionService(
                    context.uow_factory, batch_service=batch_service,
                ),
            )
            report = await service.execute(
                capabilities=tuple(EvidenceCapability),
                window_start=context.as_of - EVIDENCE_WINDOW,
                window_end=context.as_of,
                max_batches=int(os.getenv("V3_EVIDENCE_MAX_BATCHES", "1")),
            )
        finally:
            await registry.close()
        failed = _evidence_failed_capabilities(report)
        if len(failed) == len(report.capabilities):
            raise RuntimeError(f"all evidence capabilities failed: {failed}")
        return {
            "capability_count": len(report.capabilities),
            "failed_capabilities": failed,
            "fetched_count": sum(
                attempt.fetched_count
                for item in report.capabilities for attempt in item.attempts
            ),
            "evidence_count": sum(
                attempt.evidence_count
                for item in report.capabilities for attempt in item.attempts
            ),
        }

    async def full_recall_handler(context) -> dict:
        """Full Recall + Raw Opportunity Publish（RT §7.2 Step 10/11）。

        优先复用同一次编排里 features Job 发布的 Feature Run
        （context.artifacts），幂等重跑/追平时退回最新 PUBLISHED run。
        RawOpportunity 与 Recall Observation 由 RunMultiRecallService
        在 publish 内一并落库。
        """
        feature_run_id = await _resolve_feature_run_id(context)
        service = RunMultiRecallService(
            context.uow_factory,
            ExchangeCalendarsAShareCalendar(),
            channels=(*feature_recall_channels(), *evidence_recall_channels()),
        )
        run = await service.execute(
            feature_run_id=UUID(feature_run_id), strategy_version="multi-recall-v1",
        )
        if run.status.value != "PUBLISHED":
            raise RuntimeError(f"multi-recall finished with status {run.status.value}")
        return {
            "recall_run_id": str(run.recall_run_id),
            "feature_run_id": str(run.feature_run_id),
            "status": run.status.value,
            "expected_channel_count": run.expected_channel_count,
            "successful_channel_count": run.successful_channel_count,
            "failed_channel_count": run.failed_channel_count,
            "hit_security_count": run.hit_security_count,
        }

    async def index_benchmarks_handler(context) -> dict:
        service = IngestIndexBenchmarksService(
            context.uow_factory, eastmoney,
            fallback_provider=TencentProvider(settings, DataQualityService()),
            clock=lambda: context.as_of,
        )
        report = await service.execute()
        published = sum(
            item["status"] == "PUBLISHED" for item in report["benchmarks"].values()
        )
        failed = sum(
            item["status"] == "FAILED" for item in report["benchmarks"].values()
        )
        if failed == len(report["benchmarks"]):
            raise RuntimeError("all index benchmark ingestions failed")
        return {
            "status": report["status"],
            "published_count": published,
            "failed_count": failed,
        }

    async def corporate_action_match_handler(context) -> dict:
        service = MatchCorporateActionsService(context.uow_factory)
        result = await service.execute(
            effective_from=context.as_of - timedelta(days=CORPORATE_ACTION_LOOKBACK_DAYS),
            effective_to=context.as_of,
        )
        return {"scanned": result.scanned, "drafts_created": result.drafts_created}

    async def projection_verify_handler(context) -> dict:
        service = VerifyPositionProjectionsService(context.uow_factory)
        report = await service.execute()
        return {
            "checked": report.checked,
            "mismatch_count": len(report.mismatches),
            "events_written": report.events_written,
        }

    async def performance_mature_handler(context) -> dict:
        service = MaturePerformanceService(
            context.uow_factory, clock=lambda: context.as_of,
        )
        return await service.execute(as_of=context.as_of)

    async def recall_observation_mature_handler(context) -> dict:
        service = MatureRecallObservationsService(
            context.uow_factory,
            BarsOutcomeProvider(context.uow_factory),
            threshold=RecallMissThreshold(
                version="scheduler-v1", raw_return_gte=0.15,
            ),
            clock=lambda: context.as_of,
        )
        result = await service.execute()
        return {
            "requested_count": result.requested_count,
            "matured_count": result.matured_count,
            "unavailable_count": result.unavailable_count,
            "evaluation_count": result.evaluation_count,
            "miss_count": result.miss_count,
            "inserted_count": result.inserted_count,
        }

    main = Orchestrator(
        uow_factory,
        (
            JobDefinition(job_id="market-data", handler=market_data_handler),
            JobDefinition(
                job_id="index-benchmarks", handler=index_benchmarks_handler,
                depends_on=("market-data",),
            ),
            JobDefinition(
                job_id="features", handler=features_handler,
                depends_on=("index-benchmarks",),
            ),
            # RT §7.2 Step 09：Evidence 增量（24h 窗口）
            JobDefinition(
                job_id="evidence-increment", handler=evidence_increment_handler,
                depends_on=("features",),
            ),
            # RT §7.2 Step 10/11：Full Recall + Raw Opportunity Publish
            JobDefinition(
                job_id="full-recall", handler=full_recall_handler,
                depends_on=("evidence-increment",),
            ),
        ),
        advisory_lock_key="v3-scheduler-main",
    )
    maintenance = Orchestrator(
        uow_factory,
        (
            JobDefinition(
                job_id="corporate-action-match",
                handler=corporate_action_match_handler,
            ),
            JobDefinition(job_id="projection-verify", handler=projection_verify_handler),
            # RT-10：Performance Mature 自动计算（正式绩效事实由系统生成）
            JobDefinition(
                job_id="performance-mature", handler=performance_mature_handler,
            ),
            # RT-10：Recall Observation Mature（Outcome Provider = 已落库日 K）
            JobDefinition(
                job_id="recall-observation-mature",
                handler=recall_observation_mature_handler,
            ),
        ),
        advisory_lock_key="v3-scheduler-maintenance",
    )
    return main, maintenance, database



async def _latest_main_success_key(database) -> str | None:
    """主链终端 Job（features）最近一次成功运行的幂等键（交易日）。"""
    async with SQLAlchemyUnitOfWork(database.sessions) as uow:
        return await uow.orchestrator.latest_succeeded_idempotency_key("features")


def _json_default(value):
    """报表里混有 datetime（如 release_resolution.resolved_at）——
    序列化时统一 isoformat，绝不让每日任务因报表落盘而 FAILED。"""
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


async def run_once(output: Path) -> dict:
    database_url = os.getenv("V3_DATABASE_URL")
    if not database_url:
        raise ValueError("V3_DATABASE_URL is required")
    main, maintenance, database = build_orchestrators(database_url)
    calendar = ExchangeCalendarsAShareCalendar()
    now = datetime.now(timezone.utc)
    local = now.astimezone(SHANGHAI)
    report: dict = {
        "started_at": now.isoformat(),
        "local_date": local.date().isoformat(),
        "trading_day": calendar.is_trading_day(local.date()),
        "calendar": {
            "source": calendar.metadata.source,
            "calendar_code": calendar.metadata.calendar_code,
            "coverage_end": calendar.metadata.coverage_end.isoformat(),
        },
    }
    # RC-07B：每次生产运行都先解析当前 Release（紧急开关关闭 → V2_FALLBACK）
    v3_enabled = os.getenv("V3_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    report["release_resolution"] = await ReleaseResolver(
        lambda: SQLAlchemyUnitOfWork(database.sessions), v3_enabled=v3_enabled,
    ).resolve("production")
    if report["trading_day"]:
        trade_date = latest_completed_session(calendar, now)
        # RT-05 catch-up：主链最近一次成功运行的交易日之后的每个交易日
        # 都要补齐（调度中断/宕机后自动追平），Orchestrator 幂等保证安全
        last_key = await _latest_main_success_key(database)
        last_completed = date.fromisoformat(last_key) if last_key else None
        pending = catchup_trade_dates(
            calendar.is_trading_day,
            last_completed=last_completed, today=trade_date,
        )
        report["catchup"] = [day.isoformat() for day in pending]
        report["main"] = [
            await main.execute(trade_date=pending_date, as_of=now)
            for pending_date in pending
        ]
    # 维护链每个自然日独立执行（幂等键 = 本地日期）
    report["maintenance"] = await maintenance.execute(
        trade_date=local.date(), as_of=now
    )
    def _run_statuses(part_report):
        if isinstance(part_report, list):
            return [run["status"] for run in part_report]
        return [part_report["status"]]

    statuses = [
        status
        for part in ("main", "maintenance")
        if part in report
        for status in _run_statuses(report[part])
    ]
    report["status"] = (
        "COMPLETED" if all(status == "COMPLETED" for status in statuses) else "PARTIAL"
    )
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    await database.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")
    temporary.replace(output)
    return report


async def run_scheduler(args: argparse.Namespace) -> int:
    while True:
        if not args.once:
            await asyncio.sleep(seconds_until_next_run(datetime.now(SHANGHAI), args.at))
        try:
            report = await run_once(args.output)
            print(json.dumps(report, ensure_ascii=False, default=_json_default), flush=True)
        except Exception as exc:
            error = str(exc)
            database_url = os.getenv("V3_DATABASE_URL")
            if database_url:
                error = error.replace(database_url, "<redacted-database-url>")
            print(
                json.dumps(
                    {
                        "status": "FAILED",
                        "error_type": type(exc).__name__,
                        "error": error[:1000],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if args.once:
                return 1
        if args.once:
            return 0


def build_intraday_loop(database) -> IntradayTriggerLoop:
    """RT §21：现有 worker 容器内的盘中触发循环（不新增容器）。"""
    settings = Settings(_env_file=None, v3_database_url=os.getenv("V3_DATABASE_URL"))
    uow_factory = lambda: SQLAlchemyUnitOfWork(database.sessions)  # noqa: E731
    calendar = ExchangeCalendarsAShareCalendar()

    def _trading_day(value):
        try:
            return bool(calendar.is_trading_day(value))
        except Exception:
            return False

    return IntradayTriggerLoop(
        uow_factory,
        IntradayMarketDataService(EastmoneyProvider(settings)),
        AttentionEngineService(uow_factory),
        _trading_day,
        interval_seconds=float(os.getenv("V3_INTRADAY_INTERVAL_SECONDS", "300")),
        clock=lambda: datetime.now(timezone.utc),
    )


async def run_resident(args: argparse.Namespace) -> int:
    """常驻模式：EOD 调度 + 盘中触发循环并发（RT §21 部署裁决）。"""
    database_url = os.getenv("V3_DATABASE_URL")
    if not database_url:
        raise ValueError("V3_DATABASE_URL is required")
    _, _, database = build_orchestrators(database_url)
    intraday_task = asyncio.create_task(build_intraday_loop(database).run_forever())
    try:
        return await run_scheduler(args)
    finally:
        intraday_task.cancel()


def main() -> int:
    args = build_parser().parse_args()
    if args.intraday_once:
        return asyncio.run(_run_intraday_once())
    if args.once:
        # --once 只跑一次 EOD（测试/手动触发语义不变），不起盘中循环
        return asyncio.run(run_scheduler(args))
    return asyncio.run(run_resident(args))


async def _run_intraday_once() -> int:
    database_url = os.getenv("V3_DATABASE_URL")
    if not database_url:
        raise ValueError("V3_DATABASE_URL is required")
    _, _, database = build_orchestrators(database_url)
    summary = await build_intraday_loop(database).evaluate_once()
    await database.close()
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
