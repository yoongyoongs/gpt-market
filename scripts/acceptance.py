from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.container import container


async def capture(label: str, operation, output: dict) -> None:
    try:
        value = await operation
        output[label] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    except Exception as exc:
        output[label] = {"ok": False, "error": str(exc)}


async def main() -> None:
    result: dict = {}
    await container.start()
    try:
        await capture("get_quote_002284", container.quotes.get_quote("002284"), result)
        await capture("get_quote_600722", container.quotes.get_quote("600722"), result)
        await capture("get_stock_detail_002284", container.klines.get_stock_detail("002284"), result)
        await capture("get_sector_ranking_industry", container.sectors.get_sector_ranking("industry", 30), result)
        await capture("scan_mainboard_top30", container.scanner.scan_mainboard(top_n=30), result)
    finally:
        await container.close()
    path = Path(__file__).resolve().parents[1] / "docs" / "acceptance_results.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    scan = result.get("scan_mainboard_top30", {})
    print(json.dumps({
        "output": str(path),
        "quote_002284": result.get("get_quote_002284", {}).get("price"),
        "quote_600722": result.get("get_quote_600722", {}).get("price"),
        "detail_ok": result.get("get_stock_detail_002284", {}).get("ok", True),
        "sector_count": len(result.get("get_sector_ranking_industry", {}).get("items", [])),
        "coverage": scan.get("coverage"),
        "candidate_count": len(scan.get("candidates", [])),
        "scan_error": scan.get("error"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
