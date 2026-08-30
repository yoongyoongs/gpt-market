from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from uuid import UUID

from app.config import get_settings
from app.utils.time import SHANGHAI
from app.v3.application.run_full_market_features import RunFullMarketFeaturesService
from app.v3.container import V3Container


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run V3 full-market daily features")
    parser.add_argument("--universe-snapshot-id", required=True)
    parser.add_argument("--as-of", help="timezone-aware ISO timestamp; defaults to latest completed close")
    parser.add_argument("--feature-version", default="full-market-v1")
    parser.add_argument("--batch-size", type=int, default=100)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    container = V3Container.from_settings(settings)
    if not container.enabled:
        raise RuntimeError("V3_ENABLED=true is required")
    try:
        await container.start()
        now = datetime.now(SHANGHAI)
        if args.as_of:
            as_of = datetime.fromisoformat(args.as_of)
            if as_of.tzinfo is None:
                raise ValueError("--as-of must include a timezone")
        else:
            as_of = now
        service = RunFullMarketFeaturesService(
            container.uow, batch_size=args.batch_size
        )
        result = await service.execute(
            universe_snapshot_id=UUID(args.universe_snapshot_id),
            as_of=as_of,
            feature_version=args.feature_version,
        )
        print(result.model_dump_json(indent=2))
    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
