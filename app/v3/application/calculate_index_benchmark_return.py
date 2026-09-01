from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.v3.domain.index_benchmark import IndexBenchmarkRevision


CALCULATION_VERSION = "index-return-20d-v1"
RETURN_WINDOW = 20


@dataclass(frozen=True)
class IndexBenchmarkReturnResult:
    benchmark_code: str
    revision_id: UUID | None
    known_at: datetime | None
    source: str | None
    calculation_version: str
    return_20d: float | None
    reason: str | None = None


class CalculateIndexBenchmarkReturn:
    """指数 20 日收益的确定性计算（RC-04-02）。

    只使用 bar_time <= as_of 且非 provisional 的完整日 K；不足窗口时返回
    None 并给出显式 reason，绝不用当前快照收益冒充历史收益。
    """

    def execute(
        self, *, revision: IndexBenchmarkRevision | None, as_of: datetime
    ) -> IndexBenchmarkReturnResult:
        if revision is None:
            return IndexBenchmarkReturnResult(
                benchmark_code="NONE", revision_id=None, known_at=None,
                source=None, calculation_version=CALCULATION_VERSION,
                return_20d=None, reason="NO_REVISION",
            )
        closes = [
            bar.close for bar in revision.bars
            if bar.bar_time <= as_of
        ]
        if len(closes) <= RETURN_WINDOW:
            return IndexBenchmarkReturnResult(
                benchmark_code=revision.benchmark_code,
                revision_id=revision.revision_id, known_at=revision.known_at,
                source=revision.source, calculation_version=CALCULATION_VERSION,
                return_20d=None, reason="INSUFFICIENT_HISTORY",
            )
        return_20d = closes[-1] / closes[-RETURN_WINDOW - 1] - 1
        return IndexBenchmarkReturnResult(
            benchmark_code=revision.benchmark_code,
            revision_id=revision.revision_id, known_at=revision.known_at,
            source=revision.source, calculation_version=CALCULATION_VERSION,
            return_20d=return_20d,
        )
