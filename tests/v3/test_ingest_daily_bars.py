from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.v3.application.ingest_daily_bars import (
    AllHistoricalBarProvidersFailed,
    BuildDailyBarRevisionsService,
)
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    HistoricalBarFetchResult,
    MarketBar,
    PointInTimePrecision,
)


NOW = datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc)


def result(source: str, adjust: AdjustType, *, count: int = 3) -> HistoricalBarFetchResult:
    bars = []
    for index in range(count):
        raw_close = 10 + index
        factor = 0.5 if index < 2 else 1.0
        close = raw_close if adjust is AdjustType.RAW else raw_close * factor
        bars.append(
            MarketBar(
                bar_time=NOW - timedelta(days=count - index),
                open=close,
                high=close + 1,
                low=max(0.1, close - 1),
                close=close,
                volume=1000,
                amount=10000,
                fetch_time=NOW,
            )
        )
    return HistoricalBarFetchResult(
        source_code=source,
        upstream_source=source,
        code="600000",
        period=BarPeriod.DAY,
        adjust_type=adjust,
        fetch_time=NOW,
        bars=tuple(bars),
    )


class FakeProvider:
    def __init__(self, code: str, *, raw_error: Exception | None = None, qfq_error=None) -> None:
        self.code = code
        self.raw_error = raw_error
        self.qfq_error = qfq_error
        self.calls: list[AdjustType] = []

    async def fetch(self, code, period, adjust_type, limit):
        self.calls.append(adjust_type)
        error = self.raw_error if adjust_type is AdjustType.RAW else self.qfq_error
        if error:
            raise error
        return result(self.code, adjust_type)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_paired_raw_and_qfq_build_factor_bound_revisions() -> None:
    security_id = uuid4()
    bundle = await BuildDailyBarRevisionsService(
        [FakeProvider("primary")], clock=lambda: NOW
    ).execute(security_id, "600000")

    assert bundle.raw_revision is not None
    assert bundle.raw_revision.adjust_type is AdjustType.RAW
    assert bundle.factor_revision is not None
    assert [round(item.factor, 6) for item in bundle.factor_revision.factors] == [0.5, 0.5, 1.0]
    assert bundle.adjusted_revision.factor_revision_id == bundle.factor_revision.factor_revision_id
    assert bundle.adjusted_revision.point_in_time_precision is PointInTimePrecision.FULL
    assert bundle.hfq_revision is not None
    assert bundle.hfq_revision.adjust_type is AdjustType.HFQ
    assert [bar.close for bar in bundle.hfq_revision.bars] == [10.0, 11.0, 24.0]
    assert bundle.hfq_revision.factor_revision_id == bundle.factor_revision.factor_revision_id


@pytest.mark.asyncio
async def test_raw_failure_prefers_later_paired_provider() -> None:
    first = FakeProvider("first", raw_error=RuntimeError("raw unavailable"))
    second = FakeProvider("second")

    bundle = await BuildDailyBarRevisionsService([first, second], clock=lambda: NOW).execute(
        uuid4(), "600000"
    )

    assert bundle.source_code == "second"
    assert bundle.raw_revision is not None
    assert "first:RAW" in bundle.provider_errors[0]


@pytest.mark.asyncio
async def test_qfq_only_is_explicitly_limited_without_fabricated_factor() -> None:
    provider = FakeProvider("only", raw_error=RuntimeError("raw unavailable"))
    bundle = await BuildDailyBarRevisionsService([provider], clock=lambda: NOW).execute(
        uuid4(), "600000"
    )

    assert bundle.raw_revision is None
    assert bundle.factor_revision is None
    assert bundle.hfq_revision is None
    assert bundle.adjusted_revision.raw_bar_available is False
    assert bundle.adjusted_revision.point_in_time_precision is PointInTimePrecision.LIMITED
    assert "RAW history was unavailable" in bundle.adjusted_revision.precision_reason


@pytest.mark.asyncio
async def test_all_qfq_sources_fail_explicitly() -> None:
    provider = FakeProvider("down", qfq_error=RuntimeError("down"))
    with pytest.raises(AllHistoricalBarProvidersFailed, match="down:QFQ"):
        await BuildDailyBarRevisionsService([provider], clock=lambda: NOW).execute(
            uuid4(), "600000"
        )


@pytest.mark.asyncio
async def test_current_unclosed_bar_is_partial_and_not_published() -> None:
    current = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)
    fetched = result("primary", AdjustType.QFQ)
    current_bar = fetched.bars[-1].model_copy(update={"bar_time": current})
    provider = FakeProvider("primary", raw_error=RuntimeError("raw unavailable"))

    async def fetch(code, period, adjust_type, limit):
        return fetched.model_copy(
            update={
                "adjust_type": adjust_type,
                "bars": (*fetched.bars[:-1], current_bar),
            }
        )

    provider.fetch = fetch
    bundle = await BuildDailyBarRevisionsService(
        [provider], clock=lambda: datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)
    ).execute(uuid4(), "600000")

    assert all(bar.bar_time != current for bar in bundle.adjusted_revision.bars)
    assert bundle.partial_bars[-1].bar_time == current
    assert bundle.partial_bars[-1].provisional is True


@pytest.mark.asyncio
async def test_stale_nonempty_series_is_rejected_and_falls_through() -> None:
    stale = FakeProvider("stale")
    fresh = FakeProvider("fresh")
    stale_fetch = stale.fetch

    async def fetch_old(code, period, adjust_type, limit):
        value = await stale_fetch(code, period, adjust_type, limit)
        return value.model_copy(
            update={
                "bars": tuple(
                    bar.model_copy(update={"bar_time": bar.bar_time - timedelta(days=20)})
                    for bar in value.bars
                )
            }
        )

    stale.fetch = fetch_old

    bundle = await BuildDailyBarRevisionsService([stale, fresh], clock=lambda: NOW).execute(
        uuid4(),
        "600000",
        minimum_last_bar_date=(NOW - timedelta(days=2)).date(),
    )

    assert bundle.source_code == "fresh"
    assert bundle.provider_errors[0].startswith("stale:QFQ:ValueError:last bar")
