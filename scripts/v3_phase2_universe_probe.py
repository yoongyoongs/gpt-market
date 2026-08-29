from __future__ import annotations

import asyncio
from collections import Counter
import json
import time

from app.v3.infrastructure.providers.universe import ExchangeUniverseProvider


async def probe() -> dict:
    provider = ExchangeUniverseProvider(timeout=30, concurrency=4, attempts=3)
    started = time.perf_counter()
    try:
        result = await provider.fetch_snapshot()
    finally:
        await provider.close()
    keys = {(member.market.value, member.code) for member in result.members}
    markets = Counter(member.market.value for member in result.members)
    return {
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


def main() -> None:
    print(json.dumps(asyncio.run(probe()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
