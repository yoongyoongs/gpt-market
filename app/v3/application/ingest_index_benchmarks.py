from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from app.v3.domain.index_benchmark import (
    IndexBenchmarkBar,
    IndexBenchmarkRevision,
    IndexBenchmarkRevisionContent,
)
from uuid import uuid4


# A 股主要基准目录（RC-04-02）：代码/市场映射为 Eastmoney 指数 secid 规则。
BENCHMARK_CATALOG: dict[str, tuple[str, str]] = {
    "HS300": ("000300", "SH"),
    "CSI500": ("000905", "SH"),
    "CSI1000": ("000852", "SH"),
    "SSE": ("000001", "SH"),
    "SZSE": ("399001", "SZ"),
    "CHINEXT": ("399006", "SZ"),
}


class IngestIndexBenchmarksService:
    """指数基准日 K 摄取（RC-04-02）。

    Provider 只提供日 K 事实；Revision append-only、内容寻址去重；
    单个基准失败不阻断其它基准（PARTIAL）。与个股摄取相同的
    known_at 语义：本次抓取完成时间。
    """

    def __init__(
        self,
        uow_factory: Callable,
        provider: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        history_limit: int = 400,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider
        self._clock = clock
        self._history_limit = history_limit

    async def execute(
        self, benchmarks: Iterable[str] = tuple(BENCHMARK_CATALOG)
    ) -> dict:
        known_at = self._clock()
        report: dict[str, dict] = {}
        for code in benchmarks:
            symbol, market = BENCHMARK_CATALOG[code]
            try:
                revision = await self._fetch_revision(
                    code, symbol, market, known_at
                )
            except Exception as exc:
                report[code] = {
                    "status": "FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                continue
            async with self._uow_factory() as uow:
                published = await uow.index_benchmarks.publish(revision)
                await uow.commit()
            report[code] = {
                "status": "PUBLISHED" if published else "UNCHANGED",
                "published": int(published),
                "revision_id": str(revision.revision_id),
                "content_hash": revision.content_hash,
                "known_at": revision.known_at,
                "bar_count": len(revision.bars),
            }
        status = (
            "COMPLETED"
            if all(item["status"] != "FAILED" for item in report.values())
            else "PARTIAL"
        )
        return {"status": status, "known_at": known_at, "benchmarks": report}

    async def _fetch_revision(
        self, code: str, symbol: str, market: str, known_at: datetime
    ) -> IndexBenchmarkRevision:
        result = await self._provider.get_index_kline(
            symbol, market, period="day", limit=self._history_limit
        )
        bars = tuple(
            IndexBenchmarkBar(
                bar_time=kline.timestamp,
                close=float(kline.close),
                amount=float(kline.amount) if kline.amount is not None else None,
            )
            for kline in result.klines
        )
        content = IndexBenchmarkRevisionContent(
            revision_id=uuid4(),
            benchmark_code=code,
            source="eastmoney",
            upstream_source="eastmoney",
            fetch_time=known_at,
            known_at=known_at,
            bars=bars,
        )
        return IndexBenchmarkRevision.build(content)
