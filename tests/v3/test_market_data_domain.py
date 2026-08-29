from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    Market,
    MarketBar,
    PointInTimePrecision,
    SecurityMember,
    UniverseSnapshot,
    UniverseSnapshotContent,
    UniverseSnapshotStatus,
)


NOW = datetime(2026, 8, 29, 7, 10, tzinfo=timezone.utc)


def member(code: str = "600000") -> SecurityMember:
    return SecurityMember(code=code, market=Market.SH, name="浦发银行")


def bar(day: int, *, provisional: bool = False) -> MarketBar:
    return MarketBar(
        bar_time=NOW - timedelta(days=day),
        open=10,
        high=11,
        low=9,
        close=10.5,
        volume=100,
        amount=1000,
        provisional=provisional,
        fetch_time=NOW,
    )


def test_universe_snapshot_hash_and_unique_members() -> None:
    content = UniverseSnapshotContent(
        snapshot_id=uuid4(),
        source_code="eastmoney",
        status=UniverseSnapshotStatus.PRIMARY,
        as_of=NOW,
        fetch_time=NOW,
        known_at=NOW,
        coverage=1,
        stale=False,
        members=(member(),),
    )
    snapshot = UniverseSnapshot.build(content)
    assert len(snapshot.content_hash) == 64

    with pytest.raises(ValidationError, match="unique"):
        UniverseSnapshotContent(**content.model_dump(exclude={"members"}), members=(member(), member()))


def test_lkg_snapshot_must_be_stale() -> None:
    with pytest.raises(ValidationError, match="LKG"):
        UniverseSnapshotContent(
            snapshot_id=uuid4(),
            source_code="lkg",
            status=UniverseSnapshotStatus.LKG,
            as_of=NOW,
            fetch_time=NOW,
            known_at=NOW,
            coverage=0.9,
            stale=False,
            members=(member(),),
        )


def test_bar_revision_requires_order_formal_bars_and_precision_reason() -> None:
    base = dict(
        revision_id=uuid4(),
        security_id=uuid4(),
        period=BarPeriod.DAY,
        adjust_type=AdjustType.QFQ,
        source="derived",
        upstream_source="eastmoney",
        raw_bar_available=False,
        point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="upstream only supplied adjusted history",
        known_at=NOW,
    )
    revision = BarSeriesRevision.build(BarSeriesRevisionContent(**base, bars=(bar(2), bar(1))))
    assert len(revision.content_hash) == 64

    with pytest.raises(ValidationError, match="strictly increasing"):
        BarSeriesRevisionContent(**base, bars=(bar(1), bar(2)))
    with pytest.raises(ValidationError, match="provisional"):
        BarSeriesRevisionContent(**base, bars=(bar(1, provisional=True),))
    with pytest.raises(ValidationError, match="precision_reason"):
        BarSeriesRevisionContent(**{**base, "precision_reason": None}, bars=(bar(1),))


def test_market_bar_rejects_invalid_ohlc() -> None:
    with pytest.raises(ValidationError, match="high"):
        MarketBar(
            bar_time=NOW,
            open=10,
            high=9,
            low=8,
            close=10,
            volume=1,
            amount=1,
            fetch_time=NOW,
        )
