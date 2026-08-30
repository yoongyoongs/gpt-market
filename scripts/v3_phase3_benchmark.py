from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from time import perf_counter
from uuid import UUID

from app.config import get_settings
from app.v3.container import V3Container
from app.v3.domain.features import FeatureQuery, FeatureSortField


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark V3 full-market feature queries")
    parser.add_argument("--feature-run-id", required=True)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--limit", type=int, default=100)
    return parser.parse_args()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


async def run(args: argparse.Namespace) -> None:
    if args.iterations < 2:
        raise ValueError("--iterations must be at least 2")
    settings = get_settings()
    container = V3Container.from_settings(settings)
    if not container.enabled:
        raise RuntimeError("V3_ENABLED=true is required")
    run_id = UUID(args.feature_run_id)
    scenarios = {
        "code_asc": FeatureQuery(
            feature_run_id=run_id,
            sort_by=FeatureSortField.CODE,
            fields=("code", "market", "name", "coverage"),
            limit=args.limit,
        ),
        "return_20d_desc": FeatureQuery(
            feature_run_id=run_id,
            sort_by=FeatureSortField.RETURN_20D,
            descending=True,
            fields=("code", "return_20d", "coverage"),
            limit=args.limit,
        ),
        "position_60d_sh": FeatureQuery(
            feature_run_id=run_id,
            market="SH",
            sort_by=FeatureSortField.POSITION_60D,
            descending=True,
            fields=("code", "position_60d", "amount"),
            limit=args.limit,
        ),
        "return_20d_range": FeatureQuery(
            feature_run_id=run_id,
            sort_by=FeatureSortField.RETURN_20D,
            min_value=-0.1,
            max_value=0.1,
            fields=("code", "return_20d"),
            limit=args.limit,
        ),
        "coverage_asc": FeatureQuery(
            feature_run_id=run_id,
            sort_by=FeatureSortField.COVERAGE,
            fields=("code", "coverage", "stale"),
            limit=args.limit,
        ),
    }
    report: dict[str, object] = {
        "feature_run_id": str(run_id),
        "iterations": args.iterations,
        "limit": args.limit,
        "scenarios": {},
    }
    try:
        await container.start()
        for name, query in scenarios.items():
            timings: list[float] = []
            total_count = 0
            has_cursor = False
            for _ in range(args.iterations + 1):
                started = perf_counter()
                async with container.uow() as uow:
                    page = await uow.features.query(query)
                elapsed_ms = (perf_counter() - started) * 1000
                if page is None:
                    raise RuntimeError(f"feature run unavailable for {name}")
                total_count = page.total_count
                has_cursor = page.next_cursor is not None
                if timings:
                    timings.append(elapsed_ms)
                else:
                    timings = [elapsed_ms]
            samples = timings[1:]
            report["scenarios"][name] = {
                "total_count": total_count,
                "has_cursor": has_cursor,
                "p50_ms": round(statistics.median(samples), 3),
                "p95_ms": round(percentile(samples, 0.95), 3),
                "max_ms": round(max(samples), 3),
            }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
