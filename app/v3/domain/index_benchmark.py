from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


class IndexBenchmarkStatus(StrEnum):
    PUBLISHED = "PUBLISHED"


class IndexBenchmarkBar(V3Contract):
    bar_time: datetime
    close: float
    amount: float | None = None


class IndexBenchmarkRevisionContent(V3Contract):
    """指数基准 Revision 输入（RC-04-02）。

    指数基准是与个股无关的市场事实：独立 revision 链、内容寻址去重、
    append-only；只保存 Provider 提供的日 K 事实（bar_time/close/amount）。
    """

    revision_id: UUID
    benchmark_code: str
    source: str
    upstream_source: str
    fetch_time: datetime
    known_at: datetime
    bars: tuple[IndexBenchmarkBar, ...]

    @field_validator("fetch_time", "known_at")
    @classmethod
    def _require_aware(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    @model_validator(mode="after")
    def _validate_bars(self) -> "IndexBenchmarkRevisionContent":
        if not self.bars:
            raise ValueError("index benchmark revision requires at least one bar")
        times = [bar.bar_time for bar in self.bars]
        if any(later <= earlier for earlier, later in zip(times, times[1:])):
            raise ValueError("index benchmark bars must be strictly time-ordered and unique")
        return self


class IndexBenchmarkRevision(V3Contract):
    revision_id: UUID
    benchmark_code: str
    source: str
    upstream_source: str
    fetch_time: datetime
    known_at: datetime
    status: IndexBenchmarkStatus
    bars: tuple[IndexBenchmarkBar, ...]
    content_hash: str

    @classmethod
    def build(cls, content: IndexBenchmarkRevisionContent) -> "IndexBenchmarkRevision":
        # 内容寻址去重不含 revision_id：同一 known_at 重抓同一份日 K 视为
        # UNCHANGED，而不是新 revision。
        return cls(
            revision_id=content.revision_id,
            benchmark_code=content.benchmark_code,
            source=content.source,
            upstream_source=content.upstream_source,
            fetch_time=content.fetch_time,
            known_at=content.known_at,
            status=IndexBenchmarkStatus.PUBLISHED,
            bars=content.bars,
            content_hash=canonical_hash(
                content.model_dump(mode="json", exclude={"revision_id"})
            ),
        )
