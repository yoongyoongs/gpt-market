from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from uuid import UUID

from app.config import get_settings
from app.utils.time import now_shanghai
from app.v3.application.run_evidence_ingestion import RunEvidenceIngestionService
from app.v3.application.run_evidence_registry import (
    CapabilityRunStatus,
    RunEvidenceRegistryService,
)
from app.v3.container import V3Container
from app.v3.infrastructure.providers.evidence import (
    CninfoAnnouncementParser,
    CninfoAnnouncementProvider,
    EastmoneyNewsParser,
    EastmoneyNewsProvider,
    EastmoneyReportParser,
    EastmoneyReportProvider,
    EvidenceProviderRegistry,
    GovernmentPolicyParser,
    GovernmentPolicyProvider,
    SseAnnouncementParser,
    SseAnnouncementProvider,
)
from app.v3.providers.evidence import EvidenceCapability


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("evidence job timestamps must include a timezone")
    return parsed


def parse_resume(values: list[str]) -> dict[str, UUID]:
    result = {}
    for value in values:
        source, separator, run_id = value.partition("=")
        if not separator or not source:
            raise ValueError("--resume must use SOURCE=FETCH_RUN_UUID")
        result[source] = UUID(run_id)
    return result


def build_registry(codes: tuple[str, ...]) -> EvidenceProviderRegistry:
    registry = EvidenceProviderRegistry()
    registry.register(
        EvidenceCapability.ANNOUNCEMENT,
        CninfoAnnouncementProvider(),
        CninfoAnnouncementParser(),
    )
    registry.register(
        EvidenceCapability.ANNOUNCEMENT,
        SseAnnouncementProvider(),
        SseAnnouncementParser(),
    )
    report_parser = EastmoneyReportParser()
    registry.register(
        EvidenceCapability.FINANCIAL,
        EastmoneyReportProvider(
            report_name="RPT_F10_FINANCE_MAINFINADATA", codes=codes, chunk_size=50
        ),
        report_parser,
    )
    registry.register(
        EvidenceCapability.PERFORMANCE,
        EastmoneyReportProvider(
            report_name="RPT_PUBLIC_OP_NEWPREDICT", codes=codes, chunk_size=50
        ),
        report_parser,
    )
    registry.register(
        EvidenceCapability.PERFORMANCE,
        EastmoneyReportProvider(
            report_name="RPT_FCI_PERFORMANCEE", codes=codes, chunk_size=50
        ),
        report_parser,
    )
    registry.register(
        EvidenceCapability.POLICY,
        GovernmentPolicyProvider(),
        GovernmentPolicyParser(),
    )
    registry.register(
        EvidenceCapability.NEWS,
        EastmoneyNewsProvider(),
        EastmoneyNewsParser(),
    )
    return registry


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    container = V3Container.from_settings(settings)
    if not container.enabled:
        raise RuntimeError("V3_ENABLED=true is required")
    end = parse_time(args.window_end) if args.window_end else now_shanghai()
    start = parse_time(args.window_start) if args.window_start else end - timedelta(days=1)
    registry = None
    try:
        await container.start()
        async with container.uow() as uow:
            snapshot = await uow.universes.latest()
        if snapshot is None:
            raise RuntimeError("a published V3 universe snapshot is required")
        codes = tuple(member.code for member in snapshot.members)
        registry = build_registry(codes)
        service = RunEvidenceRegistryService(
            registry,
            RunEvidenceIngestionService(container.uow),
        )
        result = await service.execute(
            capabilities=tuple(EvidenceCapability),
            window_start=start,
            window_end=end,
            fetch_run_ids=parse_resume(args.resume),
            max_batches=args.max_batches,
            collect_all=args.collect_all,
        )
        print(result.model_dump_json(indent=2))
        return 1 if any(
            item.status in {CapabilityRunStatus.FAILED, CapabilityRunStatus.UNAVAILABLE}
            for item in result.capabilities
        ) else 0
    finally:
        if registry is not None:
            await registry.close()
        await container.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run V3 Phase 4 Core evidence ingestion")
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--collect-all", action="store_true")
    parser.add_argument("--resume", action="append", default=[])
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
