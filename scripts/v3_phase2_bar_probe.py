from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import json
import time
from uuid import uuid4

from app.config import Settings
from app.providers.eastmoney import EastmoneyProvider
from app.providers.tencent import TencentProvider
from app.services.data_quality import DataQualityService
from app.v3.application.ingest_daily_bars import BuildDailyBarRevisionsService
from app.v3.infrastructure.providers.bars import LegacyHistoricalBarProvider, SinaHistoricalBarProvider


DEFAULT_CODES = (
    "600000",
    "601398",
    "603019",
    "605117",
    "688981",
    "000001",
    "000858",
    "002284",
    "002594",
    "003816",
    "300750",
    "300059",
    "300308",
    "301269",
    "301236",
    "920047",
    "920799",
    "920982",
    "920185",
    "920593",
)


async def probe(codes: tuple[str, ...], *, limit: int, concurrency: int) -> dict:
    settings = Settings(_env_file=None)
    quality = DataQualityService(
        settings.stale_after_seconds,
        settings.old_after_seconds,
        settings.unavailable_after_seconds,
    )
    eastmoney = EastmoneyProvider(settings)
    tencent = TencentProvider(settings, quality)
    sina = SinaHistoricalBarProvider()
    service = BuildDailyBarRevisionsService(
        (
            LegacyHistoricalBarProvider("eastmoney", eastmoney),
            LegacyHistoricalBarProvider("tencent", tencent),
            sina,
        )
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def one(code: str) -> dict:
        async with semaphore:
            started = time.perf_counter()
            try:
                bundle = await service.execute(
                    uuid4(),
                    code,
                    limit=limit,
                    minimum_last_bar_date=datetime.now().date() - timedelta(days=10),
                )
                return {
                    "code": code,
                    "ok": True,
                    "source": bundle.source_code,
                    "precision": bundle.adjusted_revision.point_in_time_precision.value,
                    "raw": bundle.raw_revision is not None,
                    "factor": bundle.factor_revision is not None,
                    "bars": len(bundle.adjusted_revision.bars),
                    "last_bar": bundle.adjusted_revision.bars[-1].bar_time.date().isoformat(),
                    "provider_errors": list(bundle.provider_errors),
                    "seconds": round(time.perf_counter() - started, 3),
                }
            except Exception as exc:
                return {
                    "code": code,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "seconds": round(time.perf_counter() - started, 3),
                }

    started = time.perf_counter()
    try:
        rows = await asyncio.gather(*(one(code) for code in codes))
    finally:
        await eastmoney.close()
        await tencent.close()
        await sina.close()
    successes = [row for row in rows if row["ok"]]
    return {
        "total": len(rows),
        "success": len(successes),
        "success_rate": round(len(successes) / len(rows), 6),
        "eastmoney_primary": sum(row.get("source") == "eastmoney" for row in successes),
        "tencent_takeover": sum(row.get("source") == "tencent" for row in successes),
        "sina_takeover": sum(row.get("source") == "sina" for row in successes),
        "full": sum(row.get("precision") == "FULL" for row in successes),
        "limited": sum(row.get("precision") == "LIMITED" for row in successes),
        "raw_available": sum(bool(row.get("raw")) for row in successes),
        "factor_available": sum(bool(row.get("factor")) for row in successes),
        "average_seconds": round(sum(row["seconds"] for row in rows) / len(rows), 3),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe V3 Phase 2 raw/QFQ daily bar capability")
    parser.add_argument("codes", nargs="*", default=DEFAULT_CODES)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if not args.codes:
        parser.error("at least one stock code is required")
    print(
        json.dumps(
            asyncio.run(probe(tuple(args.codes), limit=args.limit, concurrency=args.concurrency)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
