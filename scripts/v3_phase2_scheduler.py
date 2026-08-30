from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, time, timedelta
import json
import os
from pathlib import Path

from app.utils.time import SHANGHAI
from scripts.v3_phase2_market_job import build_parser as build_job_parser
from scripts.v3_phase2_market_job import execute


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Schedule the V3 Phase 2 market-data job")
    parser.add_argument(
        "--at", default=os.getenv("V3_PHASE2_SCHEDULE_AT", "18:30"), type=_schedule_time
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path(os.getenv("V3_PHASE2_REPORT_PATH", "/tmp/v3-phase2-last.json"))
    )
    return parser


def _schedule_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("schedule time must be HH:MM or HH:MM:SS") from exc
    if parsed.tzinfo is not None:
        raise argparse.ArgumentTypeError("schedule time must not include a timezone")
    return parsed


def seconds_until_next_run(now: datetime, scheduled: time) -> float:
    local = now.astimezone(SHANGHAI)
    target = datetime.combine(local.date(), scheduled, tzinfo=SHANGHAI)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


async def run_once(output: Path) -> dict:
    args = build_job_parser().parse_args(["--mode", "all"])
    report = await execute(args)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return report


async def run_scheduler(args: argparse.Namespace) -> int:
    while True:
        if not args.once:
            await asyncio.sleep(seconds_until_next_run(datetime.now(SHANGHAI), args.at))
        try:
            report = await run_once(args.output)
            print(json.dumps(report, ensure_ascii=False), flush=True)
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
            return 0 if report.get("status") == "COMPLETED" else 1


def main() -> int:
    return asyncio.run(run_scheduler(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
