"""RT-03：Intraday Overlay + Lightweight Scanner（实时方案 §4.1 L1 / §5 / §27）。

- Overlay 复用最近一次 Published EOD Feature，叠加实时 Quote，绝不重算 250 日特征；
- 特征/Levels 缺失的字段诚实 None，绝不编造；
- Scanner 只用轻指标产出 IntradayAttentionCandidate，不产最终买入名单；
- stale Quote 不进扫描结果（数据质量优先）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.v3.application.intraday_overlay import (
    ActiveIntradayUniverseService,
    IntradayOverlayService,
    IntradayScannerService,
)
from app.v3.domain.features import PublishedSecurityFeatureView
from app.v3.domain.intraday import (
    ActivePoolEntry,
    IntradayAttentionCandidate,
    IntradayQuoteSnapshot,
)

NOW = datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc)


def _quote(code: str = "000001", *, price: float = 9.9, prev_close: float = 9.2,
           high: float = 9.95, low: float = 9.15, volume_ratio: float | None = 1.1,
           stale: bool = False) -> IntradayQuoteSnapshot:
    return IntradayQuoteSnapshot(
        code=code, market="SZ", name="测试",
        last_price=price, open=9.21, high=high, low=low,
        prev_close=prev_close, change=price - prev_close,
        change_pct=(price / prev_close - 1) * 100,
        volume=1_234_567, amount=11_500_000.0,
        turnover_rate=2.5, volume_ratio=volume_ratio,
        event_time=NOW - timedelta(seconds=3),
        fetch_time=NOW - timedelta(seconds=1),
        known_at=NOW - timedelta(seconds=1), as_of=NOW,
        source="eastmoney", upstream_source="eastmoney",
        quality="STALE" if stale else "LIVE", stale=stale,
        confidence="HIGH",
    )


def _feature(**overrides) -> PublishedSecurityFeatureView:
    from uuid import uuid4

    values = dict(
        feature_run_id=uuid4(), security_id=uuid4(), series_revision_id=uuid4(),
        as_of=NOW - timedelta(days=1), close=9.2, atr_pct=0.03,
        coverage=1.0, stale=False,
        features={
            "ma5": 9.0, "ma10": 8.8, "ma20": 8.5, "ma60": 8.0,
            "volume_ratio_5d": 1.2, "relative_index_strength": 0.01,
        },
        input_hash="a" * 64, source_content_hash="b" * 64,
    )
    values.update(overrides)
    return PublishedSecurityFeatureView(**values)


LEVELS = {
    "prev_high_20d": 9.85,
    "prev_low_20d": 8.9,
    "support": 9.15,
    "resistance": 9.85,
}


def test_overlay_combines_quote_feature_and_levels() -> None:
    service = IntradayOverlayService()
    overlay = service.build(
        code="000001", market="SZ",
        quote=_quote(price=9.88, prev_close=9.2, high=9.95, low=9.15),
        feature=_feature(), levels=LEVELS, as_of=NOW,
    )
    assert overlay.latest_price == 9.88
    assert overlay.intraday_return == pytest.approx(9.88 / 9.2 - 1)
    assert overlay.intraday_range_pct == pytest.approx((9.95 - 9.15) / 9.2)
    # vs MA：复用 EOD 特征均线，不重算
    assert overlay.vs_ma5 == pytest.approx(9.88 / 9.0 - 1)
    assert overlay.vs_ma20 == pytest.approx(9.88 / 8.5 - 1)
    # 突破前高（9.88 > 9.85）且贴近压力位 → 突破中
    assert overlay.breakout_now is True
    assert overlay.near_resistance is True
    assert overlay.intraday_volume_ratio == 1.1
    assert overlay.feature_as_of == NOW - timedelta(days=1)
    assert overlay.stale is False


def test_overlay_missing_inputs_are_honestly_none() -> None:
    service = IntradayOverlayService()
    # 无特征、无 Levels：派生字段一律 None，绝不编造
    overlay = service.build(
        code="600000", market="SH", quote=_quote(code="600000"),
        feature=None, levels=None, as_of=NOW,
    )
    assert overlay.vs_ma5 is None
    assert overlay.vs_ma20 is None
    assert overlay.vs_prev_high_20d is None
    assert overlay.breakout_now is None
    assert overlay.near_support is None
    assert overlay.intraday_return is not None  # 仅来自 Quote 的事实仍可用


def test_failed_breakout_touched_high_then_fell_back() -> None:
    service = IntradayOverlayService()
    # 盘中最高 9.95 越过前高 9.85，现价 9.8 跌回 → 假突破
    overlay = service.build(
        code="000001", market="SZ",
        quote=_quote(price=9.8, high=9.95, low=9.7),
        feature=_feature(), levels=LEVELS, as_of=NOW,
    )
    assert overlay.failed_breakout is True
    assert overlay.breakout_now is False


def test_scanner_flags_volume_surge_strong_vs_index_and_sharp_drop() -> None:
    scanner = IntradayScannerService()
    quotes = [
        _quote("000001", volume_ratio=2.5),                     # 放量
        _quote("000002", price=9.2 * 1.06, volume_ratio=1.0),   # 强于指数 +6%
        _quote("000003", price=9.2 * 0.94, volume_ratio=1.0),   # 急跌 -6%
        _quote("000004", price=9.2, volume_ratio=1.0),          # 平淡：不进结果
        _quote("000005", volume_ratio=2.5, stale=True),         # stale：绝不入选
    ]
    candidates = scanner.scan(quotes, index_return=0.005, as_of=NOW)
    by_code = {c.code: c for c in candidates}
    assert isinstance(by_code["000001"], IntradayAttentionCandidate)
    assert "VOLUME_SURGE" in by_code["000001"].reasons
    assert "STRONG_VS_INDEX" in by_code["000002"].reasons
    assert "SHARP_DROP" in by_code["000003"].reasons
    assert "000004" not in by_code
    # stale Quote 不进扫描结果
    assert "000005" not in by_code
    candidate = by_code["000001"]
    assert candidate.as_of == NOW
    assert candidate.known_at is not None
    # 事件候选只陈述事实，绝无"买入建议"字段
    assert not hasattr(candidate, "recommendation")


def test_scanner_uses_overlay_flags_when_provided() -> None:
    scanner = IntradayScannerService()
    quotes = [_quote("000001")]
    overlays = {}
    service = IntradayOverlayService()
    overlay = service.build(
        code="000001", market="SZ", quote=quotes[0],
        feature=_feature(), levels=LEVELS, as_of=NOW,
    )
    overlays["000001"] = overlay
    candidates = scanner.scan(
        quotes, overlays=overlays, index_return=0.0, as_of=NOW,
    )
    by_code = {c.code: c for c in candidates}
    assert "BREAKOUT_20D" in by_code["000001"].reasons
    assert "VOLUME_SURGE" not in by_code["000001"].reasons  # 量比 1.1 未达阈值


def test_active_universe_merges_and_dedupes_with_provenance() -> None:
    service = ActiveIntradayUniverseService()
    universe = service.merge(
        eod_candidates=[("SZ", "000001"), ("SZ", "000002")],
        watchlist=[("SZ", "000002"), ("SH", "600000")],
        portfolio=[("SH", "600000"), ("SZ", "300750")],
        intraday_attention=[("SZ", "000003")],
    )
    by_key = {(entry.market, entry.code): entry for entry in universe}
    assert len(universe) == 5  # 去重后
    assert set(by_key[("SZ", "000002")].sources) == {"EOD_CANDIDATE", "WATCHLIST"}
    assert set(by_key[("SH", "600000")].sources) == {"WATCHLIST", "PORTFOLIO"}
    assert set(by_key[("SZ", "000003")].sources) == {"INTRADAY_ATTENTION"}
    assert all(isinstance(entry, ActivePoolEntry) for entry in universe)
