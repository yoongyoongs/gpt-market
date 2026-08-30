from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from uuid import UUID

from app.config import Settings
from app.providers.eastmoney import EastmoneyProvider
from app.providers.tencent import TencentProvider
from app.services.data_quality import DataQualityService
from app.v3.application.aggregate_daily_bars import AggregateDailyBarsService
from app.v3.application.backfill_daily_bars import BackfillDailyBarsService
from app.v3.application.ingest_daily_bars import BuildDailyBarRevisionsService
from app.v3.application.ingest_corporate_actions import IngestCorporateActionsService
from app.v3.application.publish_bar_bundle import PublishBarBundleService
from app.v3.application.refresh_universe import RefreshUniverseService
from app.v3.infrastructure.db.session import V3Database
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.infrastructure.providers.bars import (
    CircuitBreakingHistoricalBarProvider,
    LegacyHistoricalBarProvider,
    SinaHistoricalBarProvider,
)
from app.v3.infrastructure.providers.exchange_calendar import ExchangeCalendarsAShareCalendar
from app.v3.infrastructure.providers.corporate_actions import (
    EastmoneyCorporateActionProvider,
)
from app.v3.infrastructure.providers.universe import (
    ExchangeUniverseProvider,
    LegacyUniverseProvider,
    OfficialUniverseWithVendorStatusProvider,
)
from app.v3.jobs.market_data import latest_completed_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the V3 Phase 2 market-data job")
    parser.add_argument(
        "--mode", choices=("universe", "backfill", "corporate-actions", "all"), default="all"
    )
    parser.add_argument("--database-url", default=os.getenv("V3_DATABASE_URL"))
    parser.add_argument("--run-id", type=UUID)
    parser.add_argument(
        "--limit", type=int, default=int(os.getenv("V3_PHASE2_HISTORY_LIMIT", "300"))
    )
    parser.add_argument(
        "--concurrency", type=int, default=int(os.getenv("V3_PHASE2_CONCURRENCY", "16"))
    )
    parser.add_argument("--stop-after", type=int)
    parser.add_argument("--minimum-last-bar-date", type=date.fromisoformat)
    parser.add_argument("--corporate-since", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--lock-key", type=int, default=int(os.getenv("V3_PHASE2_LOCK_KEY", "33020001"))
    )
    return parser


def _run_report(run) -> dict:
    return {
        "run_id": str(run.run_id),
        "universe_snapshot_id": str(run.universe_snapshot_id),
        "status": run.status.value,
        "expected_count": run.expected_count,
        "processed_count": run.processed_count,
        "successful_count": run.successful_count,
        "failed_count": run.failed_count,
        "errors": list(run.errors),
        "cursor": run.cursor,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


async def execute(args: argparse.Namespace) -> dict:
    if not args.database_url:
        raise ValueError("V3_DATABASE_URL or --database-url is required")
    if args.mode in {"universe", "corporate-actions"} and args.run_id is not None:
        raise ValueError("--run-id is only valid for backfill or all mode")
    settings = Settings(_env_file=None, v3_database_url=args.database_url)
    database = V3Database(
        args.database_url,
        echo=settings.v3_database_echo,
        pool_size=settings.v3_database_pool_size,
        max_overflow=settings.v3_database_max_overflow,
    )

    def uow_factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(database.sessions)

    quality = DataQualityService(
        settings.stale_after_seconds,
        settings.old_after_seconds,
        settings.unavailable_after_seconds,
    )
    eastmoney = EastmoneyProvider(settings)
    tencent = TencentProvider(settings, quality)
    sina = SinaHistoricalBarProvider()
    exchanges = ExchangeUniverseProvider()
    corporate_actions = EastmoneyCorporateActionProvider()
    calendar = ExchangeCalendarsAShareCalendar()
    report: dict = {
        "status": "RUNNING",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "calendar": {
            "source": calendar.metadata.source,
            "source_version": calendar.metadata.source_version,
            "calendar_code": calendar.metadata.calendar_code,
            "coverage_start": calendar.metadata.coverage_start.isoformat(),
            "coverage_end": calendar.metadata.coverage_end.isoformat(),
        },
    }
    started = time.perf_counter()
    try:
        await database.check_connection()
        await database.acquire_advisory_lock(args.lock_key)
        if args.mode in {"universe", "all"}:
            vendor_universe = LegacyUniverseProvider(eastmoney)
            refreshed = await RefreshUniverseService(
                uow_factory,
                (
                    OfficialUniverseWithVendorStatusProvider(exchanges, vendor_universe),
                    vendor_universe,
                ),
            ).execute()
            report["universe"] = {
                "snapshot_id": str(refreshed.snapshot.snapshot_id),
                "status": refreshed.snapshot.status.value,
                "source": refreshed.snapshot.source_code,
                "created": refreshed.created,
                "stale": refreshed.snapshot.stale,
                "coverage": refreshed.snapshot.coverage,
                "member_count": len(refreshed.snapshot.members),
                "markets": dict(
                    Counter(member.market.value for member in refreshed.snapshot.members)
                ),
                "provider_errors": list(refreshed.provider_errors),
            }
        if args.mode in {"backfill", "all"}:
            minimum_date = args.minimum_last_bar_date or latest_completed_session(
                calendar, datetime.now(timezone.utc)
            )
            publisher = PublishBarBundleService(uow_factory)
            runner = BackfillDailyBarsService(
                uow_factory,
                BuildDailyBarRevisionsService(
                    (
                        CircuitBreakingHistoricalBarProvider(
                            LegacyHistoricalBarProvider("eastmoney", eastmoney)
                        ),
                        LegacyHistoricalBarProvider("tencent", tencent),
                        sina,
                    )
                ),
                AggregateDailyBarsService(calendar),
                publisher,
            )
            run = await runner.execute(
                run_id=args.run_id,
                limit=args.limit,
                minimum_last_bar_date=minimum_date,
                stop_after=args.stop_after,
                concurrency=args.concurrency,
            )
            report["minimum_last_bar_date"] = minimum_date.isoformat()
            report["backfill"] = _run_report(run)
        if args.mode in {"corporate-actions", "all"}:
            corporate_since = args.corporate_since or (
                datetime.now(timezone.utc).date() - timedelta(days=400)
            )
            ingested = await IngestCorporateActionsService(
                uow_factory, (corporate_actions,)
            ).execute(corporate_since)
            report["corporate_actions"] = {
                "source": ingested.source_code,
                "since": corporate_since.isoformat(),
                "fetched_count": ingested.fetched_count,
                "published_count": ingested.published_count,
                "unchanged_count": ingested.unchanged_count,
                "outside_universe_count": ingested.outside_universe_count,
                "provider_errors": list(ingested.provider_errors),
            }
        report["status"] = (
            "PARTIAL" if report.get("backfill", {}).get("status") in {"PARTIAL", "FAILED"} else "COMPLETED"
        )
        return report
    finally:
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        report["wall_seconds"] = round(time.perf_counter() - started, 3)
        await exchanges.close()
        await corporate_actions.close()
        await sina.close()
        await tencent.close()
        await eastmoney.close()
        await database.close()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        report = asyncio.run(execute(args))
    except Exception as exc:
        error = str(exc)
        if args.database_url:
            error = error.replace(args.database_url, "<redacted-database-url>")
        report = {
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "error": error[:1000],
        }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.replace(args.output)
    return 0 if report["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
