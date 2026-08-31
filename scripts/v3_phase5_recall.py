from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from app.config import get_settings
from app.v3.application.evaluate_recall_channels import feature_recall_channels
from app.v3.application.evaluate_evidence_recall_channels import evidence_recall_channels
from app.v3.application.run_multi_recall import RunMultiRecallService
from app.v3.container import V3Container
from app.v3.infrastructure.providers.exchange_calendar import ExchangeCalendarsAShareCalendar


async def run(args: argparse.Namespace) -> int:
    container = V3Container.from_settings(get_settings())
    if not container.enabled:
        raise RuntimeError("V3_ENABLED=true is required")
    try:
        await container.start()
        feature_run_id = UUID(args.feature_run_id) if args.feature_run_id else None
        if feature_run_id is None:
            async with container.uow() as uow:
                latest = await uow.features.latest_run()
            if latest is None:
                raise RuntimeError("a published V3 feature run is required")
            feature_run_id = latest.feature_run_id
        service = RunMultiRecallService(
            container.uow,
            ExchangeCalendarsAShareCalendar(),
            channels=(*feature_recall_channels(), *evidence_recall_channels()),
        )
        result = await service.execute(
            feature_run_id=feature_run_id,
            strategy_version=args.strategy_version,
        )
        print(result.model_dump_json(indent=2))
        return 0 if result.status.value == "PUBLISHED" else 1
    finally:
        await container.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run V3 Phase 5 multi-recall")
    parser.add_argument("--feature-run-id")
    parser.add_argument("--strategy-version", default="multi-recall-v1")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
