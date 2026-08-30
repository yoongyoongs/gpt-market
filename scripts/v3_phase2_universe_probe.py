from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import time

from app.config import Settings
from app.providers.eastmoney import EastmoneyProvider
from app.v3.infrastructure.providers.universe import ExchangeUniverseProvider, LegacyUniverseProvider


async def probe(*, compare_primary: bool = False) -> dict:
    provider = ExchangeUniverseProvider(timeout=30, concurrency=4, attempts=3)
    started = time.perf_counter()
    try:
        result = await provider.fetch_snapshot()
    finally:
        await provider.close()
    keys = {(member.market.value, member.code) for member in result.members}
    markets = Counter(member.market.value for member in result.members)
    report = {
        "expected": result.expected_total,
        "parsed": len(result.members),
        "coverage": round(len(result.members) / result.expected_total, 6),
        "markets": dict(markets),
        "unique": len(keys),
        "current_bse_codes": sum(
            member.market.value == "BJ" and member.code.startswith("920")
            for member in result.members
        ),
        "seconds": round(time.perf_counter() - started, 3),
    }
    if compare_primary:
        eastmoney = EastmoneyProvider(Settings(_env_file=None))
        try:
            primary = await LegacyUniverseProvider(eastmoney).fetch_snapshot()
        finally:
            await eastmoney.close()
        primary_by_key = {
            (member.market.value, member.code): member for member in primary.members
        }
        official_by_key = {
            (member.market.value, member.code): member for member in result.members
        }
        primary_only = sorted(primary_by_key.keys() - official_by_key.keys())
        official_only = sorted(official_by_key.keys() - primary_by_key.keys())
        report["primary_comparison"] = {
            "primary_count": len(primary_by_key),
            "primary_markets": dict(
                Counter(member.market.value for member in primary.members)
            ),
            "primary_only_count": len(primary_only),
            "primary_only_markets": dict(Counter(market for market, _ in primary_only)),
            "primary_only_sample": [
                {"market": market, "code": code, "name": primary_by_key[(market, code)].name}
                for market, code in primary_only[:30]
            ],
            "official_only_count": len(official_only),
            "official_only_markets": dict(Counter(market for market, _ in official_only)),
            "official_only_sample": [
                {"market": market, "code": code, "name": official_by_key[(market, code)].name}
                for market, code in official_only[:30]
            ],
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe the official V3 A-share universe")
    parser.add_argument("--compare-primary", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(probe(compare_primary=args.compare_primary)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
