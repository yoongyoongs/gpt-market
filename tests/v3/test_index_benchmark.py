from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.v3.domain.index_benchmark import (
    IndexBenchmarkBar,
    IndexBenchmarkRevision,
    IndexBenchmarkRevisionContent,
)
from app.v3.application.calculate_index_benchmark_return import (
    CalculateIndexBenchmarkReturn,
)


NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


def content(
    code: str = "HS300", closes: list[float] | None = None,
    revision_id: object = None,
) -> IndexBenchmarkRevisionContent:
    values = closes if closes is not None else [
        3800 + index * 5 + (index % 4) for index in range(60)
    ]
    bars = tuple(
        IndexBenchmarkBar(
            bar_time=NOW - timedelta(days=len(values) - index),
            close=close, amount=1e11 + index * 1e9,
        )
        for index, close in enumerate(values)
    )
    return IndexBenchmarkRevisionContent(
        revision_id=revision_id or uuid4(), benchmark_code=code, source="eastmoney",
        upstream_source="eastmoney", fetch_time=NOW, known_at=NOW, bars=bars,
    )


def test_index_revision_build_is_content_hashed_and_deterministic() -> None:
    revision = IndexBenchmarkRevision.build(content())
    replay = IndexBenchmarkRevision.build(content(revision_id=revision.revision_id))
    assert revision.content_hash == replay.content_hash
    assert revision.status.value == "PUBLISHED"
    assert revision.bars[0].bar_time < revision.bars[-1].bar_time


def test_index_return_20d_is_deterministic_and_explicit_when_insufficient() -> None:
    revision = IndexBenchmarkRevision.build(content())
    calculator = CalculateIndexBenchmarkReturn()
    result = calculator.execute(revision=revision, as_of=NOW)
    assert result.return_20d is not None
    assert result.calculation_version == "index-return-20d-v1"
    assert result.benchmark_code == "HS300"
    assert result.revision_id == revision.revision_id
    assert result.known_at == revision.known_at
    replay = calculator.execute(revision=revision, as_of=NOW)
    assert result.return_20d == replay.return_20d

    short = IndexBenchmarkRevision.build(content(closes=[3800.0 + index for index in range(10)]))
    empty = calculator.execute(revision=short, as_of=NOW)
    assert empty.return_20d is None
    assert empty.reason == "INSUFFICIENT_HISTORY"

    late_only = IndexBenchmarkRevision.build(content(
        closes=[3800.0 + index for index in range(40)],
    ))
    filtered = calculator.execute(
        revision=late_only, as_of=NOW - timedelta(days=100),
    )
    assert filtered.return_20d is None


def test_index_revision_rejects_unsorted_or_duplicate_bars() -> None:
    duplicated = content(closes=[3800.0 + index for index in range(30)]).model_copy(
        update={"bars": content(closes=[3800.0 + index for index in range(30)]).bars[:-1]
                + content(closes=[3860.0, 3870.0]).bars},
    )
    with pytest.raises(ValueError):
        IndexBenchmarkRevisionContent.model_validate(duplicated.model_dump(mode="json"))
