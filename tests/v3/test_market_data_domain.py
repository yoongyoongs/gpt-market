from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.v3.domain.market_data import (
    AdjustType,
    AdjustmentFactorPoint,
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


def test_universe_snapshot_hash_is_independent_of_provider_member_order() -> None:
    snapshot_id = uuid4()
    first = SecurityMember(code="600000", market=Market.SH, name="浦发银行")
    second = SecurityMember(code="000001", market=Market.SZ, name="平安银行")
    values = {
        "snapshot_id": snapshot_id,
        "source_code": "fixture",
        "status": UniverseSnapshotStatus.PRIMARY,
        "as_of": NOW,
        "fetch_time": NOW,
        "known_at": NOW,
        "coverage": 1,
        "stale": False,
    }

    ordered = UniverseSnapshot.build(UniverseSnapshotContent(**values, members=(first, second)))
    reversed_input = UniverseSnapshot.build(
        UniverseSnapshotContent(**values, members=(second, first))
    )

    assert ordered.content_hash == reversed_input.content_hash
    assert ordered.members == reversed_input.members


def test_market_data_values_are_normalized_to_database_precision() -> None:
    content = UniverseSnapshotContent(
        snapshot_id=uuid4(),
        source_code="fixture",
        status=UniverseSnapshotStatus.PRIMARY,
        as_of=NOW,
        fetch_time=NOW,
        known_at=NOW,
        coverage=0.9400508044,
        stale=False,
        members=(member(),),
    )
    value = MarketBar(
        bar_time=NOW,
        open=10.1234567,
        high=11.1234567,
        low=9.1234567,
        close=10.5234567,
        volume=100,
        amount=1234.56789,
        fetch_time=NOW,
    )

    assert content.coverage == 0.94005
    assert value.open == 10.123457
    assert value.amount == 1234.5679
    assert AdjustmentFactorPoint(trading_time=NOW, factor=1.1234567890126).factor == 1.123456789013


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
