from __future__ import annotations

# ruff: noqa: E402

import argparse
import asyncio
import json
import os
import sys
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from app.v3.application.build_candidate_comparison import (
    BuildCandidateComparisonService,
    CandidateComparisonQuery,
)
from app.v3.application.build_context_pack import (
    BuildContextPackCommand,
    BuildContextPackService,
)
from app.v3.domain.context import ContextLevel, ContextSubjectType
from app.v3.domain.features import FeatureQuery
from app.v3.domain.task import TaskProfile
from app.v3.infrastructure.db.session import V3Database
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork


PROFILE_NAMESPACE = UUID("5b63ae98-79d9-4464-a74b-864ed82d88fd")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return ordered[index]


async def measured(call, repetitions: int) -> dict[str, float]:
    samples = []
    for _ in range(repetitions):
        started = time.perf_counter()
        await call()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "count": len(samples),
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "max_ms": round(max(samples), 3),
    }


async def run(database_url: str, repetitions: int) -> dict:
    settings = Settings(_env_file=None, v3_database_url=database_url)
    database = V3Database(
        database_url,
        echo=settings.v3_database_echo,
        pool_size=settings.v3_database_pool_size,
        max_overflow=settings.v3_database_max_overflow,
    )
    def uow_factory():
        return SQLAlchemyUnitOfWork(database.sessions)

    now = datetime.now(timezone.utc)
    async with uow_factory() as uow:
        page = await uow.features.query(FeatureQuery(
            fields=("market", "code"), limit=100
        ))
    if page is None or len(page.items) < 20:
        raise RuntimeError("published feature run must expose at least 20 candidates")
    codes = tuple(f"{item['market']}:{item['code']}" for item in page.items[:100])
    comparison_query = CandidateComparisonQuery(codes=codes, as_of=now)
    comparison_service = BuildCandidateComparisonService(uow_factory, clock=lambda: now)
    comparison = await comparison_service.execute(comparison_query)

    profiles = {}
    for level in ContextLevel:
        code = f"PHASE6_ACCEPTANCE_{level.value}"
        profile = TaskProfile.build(
            task_profile_id=uuid5(PROFILE_NAMESPACE, code), profile_code=code,
            version=1, schedule=None, timezone="Asia/Shanghai",
            trading_calendar_source="exchange_calendars:XSHG",
            trading_calendar_version="acceptance", context_level=level,
            comparison_first=False, output_schema={"type": "AcceptanceOnly"},
            expected_group_count=1, grace_seconds=0,
            strategy_version="phase6-acceptance.v1",
        )
        async with uow_factory() as uow:
            await uow.task_registry.publish_profile(profile)
            await uow.commit()
        profiles[level] = profile
    target = comparison.members[0]
    context_service = BuildContextPackService(uow_factory, clock=lambda: now)
    contexts = {}
    for level, profile in profiles.items():
        contexts[level] = await context_service.execute(BuildContextPackCommand(
            context_level=level, subject_type=ContextSubjectType.SECURITY,
            subject_id=f"{target.market}:{target.code}",
            task_profile_id=profile.task_profile_id, task_profile_version=profile.version,
            as_of=now, feature_run_id=comparison.feature_run_id,
            recall_run_id=comparison.recall_run_id,
            comparison_pack_id=comparison.comparison_pack_id,
        ))

    comparison_perf = await measured(
        lambda: comparison_service.execute(comparison_query), repetitions
    )
    normal_command = BuildContextPackCommand(
        context_level=ContextLevel.NORMAL,
        subject_type=ContextSubjectType.SECURITY,
        subject_id=f"{target.market}:{target.code}",
        task_profile_id=profiles[ContextLevel.NORMAL].task_profile_id,
        task_profile_version=1, as_of=now,
        feature_run_id=comparison.feature_run_id,
        recall_run_id=comparison.recall_run_id,
        comparison_pack_id=comparison.comparison_pack_id,
    )
    context_perf = await measured(
        lambda: context_service.execute(normal_command), repetitions
    )

    async def read_context():
        async with uow_factory() as uow:
            return await uow.context_packs.get(contexts[ContextLevel.NORMAL].context_pack_id)

    read_perf = await measured(read_context, repetitions)
    await database.close()
    checks = {
        "candidate_count_20_to_100": 20 <= len(comparison.members) <= 100,
        "candidate_order_contiguous": [item.candidate_order for item in comparison.members]
        == list(range(1, len(comparison.members) + 1)),
        "no_unified_final_score": "final_total_score" not in json.dumps(
            comparison.model_dump(mode="json"), ensure_ascii=False
        ),
        "context_budgets_valid": all(
            pack.actual_tokens <= pack.token_budget for pack in contexts.values()
        ),
        "comparison_p95_under_500ms": comparison_perf["p95_ms"] < 500,
        "context_p95_under_500ms": context_perf["p95_ms"] < 500,
        "stored_read_p95_under_200ms": read_perf["p95_ms"] < 200,
    }
    return {
        "as_of": now.isoformat(),
        "database": "configured",
        "candidate_count": len(comparison.members),
        "comparison_pack_id": str(comparison.comparison_pack_id),
        "context_pack_ids": {level.value: str(pack.context_pack_id) for level, pack in contexts.items()},
        "performance": {
            "comparison": comparison_perf,
            "normal_context": context_perf,
            "stored_context_read": read_perf,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="V3 Phase 6 isolated acceptance")
    parser.add_argument("--database-url", default=os.getenv("V3_TEST_DATABASE_URL"))
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("V3_TEST_DATABASE_URL or --database-url is required")
    result = asyncio.run(run(args.database_url, args.repetitions))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
