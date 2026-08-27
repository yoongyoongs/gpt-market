from __future__ import annotations

from datetime import timedelta

from app.models import Quote, TechnicalIndicators
from app.services.scanner import at_price_limit, coverage_status, is_mainboard, is_one_price_board, is_st, scan_cache_key, score_candidate
from app.utils.time import freshness, now_shanghai


def quote(**overrides) -> Quote:
    values = {
        "code": "002284", "name": "亚太股份", "market": "SZ", "price": 9.58,
        "prev_close": 9.46, "open": 9.50, "high": 9.66, "low": 9.42,
        "pct_change": 1.27, "change": 0.12, "volume": 12_345_678,
        "amount": 186_000_000, "turnover_rate": 4.21, "volume_ratio": 1.52,
        "amplitude": 2.54, "suspended": False, **freshness(now_shanghai()),
    }
    values.update(overrides)
    return Quote(**values)


def test_st_filter() -> None:
    assert is_st("ST某某")
    assert is_st("*ST某某")
    assert is_st("退市某某")
    assert not is_st("亚太股份")


def test_board_prefix_filters() -> None:
    assert is_mainboard("000001") and is_mainboard("605001")
    assert not is_mainboard("300001")
    assert not is_mainboard("301001")
    assert not is_mainboard("688001")
    assert not is_mainboard("830001")


def test_limit_up_and_one_price_filter() -> None:
    assert at_price_limit(quote(price=11.0, prev_close=10.0), "up")
    assert at_price_limit(quote(name="ST测试", price=10.5, prev_close=10.0), "up")
    assert is_one_price_board(quote(high=11.0, low=11.0, price=11.0))


def test_scoring_is_bounded_and_rewards_not_yet_surged() -> None:
    technical = TechnicalIndicators(ma5=9.5, ma20=9.3, ma60=8.9, distance_ma20_pct=3.01, distance_high_20d_pct=-6)
    calm = score_candidate(quote(pct_change=1.5), technical, benchmark_pct=0.3)
    surged = score_candidate(quote(pct_change=5.0), technical, benchmark_pct=0.3)
    assert 0 <= calm.total_score <= 100
    assert calm.position_score > surged.position_score
    assert "涨幅尚未过高" in calm.reason


def test_coverage_bands() -> None:
    assert coverage_status(0.9) == "FULL"
    assert coverage_status(0.6) == "BROAD"
    assert coverage_status(0.59) == "PARTIAL"


def test_mcp_and_web_numeric_defaults_share_scan_cache_key() -> None:
    assert scan_cache_key(10, 5, 50_000_000, True, True, True) == scan_cache_key(
        10, 5.0, 50_000_000.0, True, True, True
    )


def test_stale_detection() -> None:
    recent = freshness(now_shanghai() - timedelta(seconds=20))
    stale = freshness(now_shanghai() - timedelta(seconds=45))
    old = freshness(now_shanghai() - timedelta(seconds=61))
    unavailable = freshness(now_shanghai() - timedelta(minutes=6))
    assert recent["stale"] is False and recent["quality"] == "LIVE"
    assert stale["stale"] is True and stale["quality"] == "STALE"
    assert old["stale"] is True and old["quality"] == "OLD"
    assert unavailable["quality"] == "UNAVAILABLE"
