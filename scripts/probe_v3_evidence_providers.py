from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from time import perf_counter

from app.utils.time import SHANGHAI, now_shanghai
from app.v3.domain.evidence import RawDocument
from app.v3.infrastructure.providers.evidence import (
    CninfoAnnouncementParser,
    CninfoAnnouncementProvider,
    EastmoneyNewsParser,
    EastmoneyNewsProvider,
    EastmoneyReportParser,
    EastmoneyReportProvider,
    GovernmentPolicyParser,
    GovernmentPolicyProvider,
)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed


async def probe(name, provider, parser, start: datetime, end: datetime) -> dict[str, object]:
    started = perf_counter()
    try:
        batch = await provider.fetch(window_start=start, window_end=end, cursor=None)
        parsed_count = 0
        first_claim = None
        first_subject = None
        for fetched in batch.documents[:3]:
            raw = RawDocument.build(
                evidence_source_id=provider.source.evidence_source_id,
                fetched=fetched,
                normalized_reference=fetched.raw_reference,
            )
            records = parser.parse(raw, provider.source).records
            parsed_count += len(records)
            if records and first_claim is None:
                first_claim = records[0].claim_key
                first_subject = records[0].subject_id
        return {
            "provider": name,
            "status": "SUCCESS",
            "documents": len(batch.documents),
            "parsed_sample": parsed_count,
            "upstream_count": batch.upstream_count,
            "exhausted": batch.exhausted,
            "next_cursor": batch.next_cursor,
            "first_claim": first_claim,
            "first_subject": first_subject,
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        }
    except Exception as exc:
        return {
            "provider": name,
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        }
    finally:
        await provider.close()


async def run(args: argparse.Namespace) -> int:
    end = parse_time(args.window_end) if args.window_end else now_shanghai()
    start = parse_time(args.window_start) if args.window_start else end - timedelta(days=7)
    codes = tuple(code.strip() for code in args.codes.split(",") if code.strip())
    targets = (
        ("cninfo", CninfoAnnouncementProvider(page_size=10), CninfoAnnouncementParser()),
        (
            "eastmoney_financial",
            EastmoneyReportProvider(
                report_name="RPT_F10_FINANCE_MAINFINADATA", codes=codes, chunk_size=len(codes)
            ),
            EastmoneyReportParser(),
        ),
        (
            "eastmoney_forecast",
            EastmoneyReportProvider(
                report_name="RPT_PUBLIC_OP_NEWPREDICT", codes=codes, chunk_size=len(codes)
            ),
            EastmoneyReportParser(),
        ),
        (
            "eastmoney_express",
            EastmoneyReportProvider(
                report_name="RPT_FCI_PERFORMANCEE", codes=codes, chunk_size=len(codes)
            ),
            EastmoneyReportParser(),
        ),
        ("gov_policy", GovernmentPolicyProvider(page_size=10), GovernmentPolicyParser()),
        ("eastmoney_news", EastmoneyNewsProvider(page_size=10), EastmoneyNewsParser()),
    )
    results = []
    for name, provider, parser in targets:
        results.append(await probe(name, provider, parser, start, end))
    output = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "codes": codes,
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if any(item["status"] != "SUCCESS" for item in results) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe V3 Core evidence providers")
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    parser.add_argument("--codes", default="600519,000001,300750")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
