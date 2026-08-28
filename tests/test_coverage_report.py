from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.api import routes
from app.api.live import LiveSnapshotCache
from app.cache import AsyncTTLCache
from app.main import api
from app.models import (
    AvailableValue,
    CoverageReport,
    IndexSnapshot,
    Kline,
    KlineResult,
    MarketBreadth,
    MarketOverview,
    Quote,
    ScanCoverage,
    ScanResult,
    SectorItem,
    SectorRanking,
)
from app.services.data_quality import DataQualityService
from app.services.scanner import ScannerService
from app.utils.time import now_shanghai


QUALITY_SERVICE = DataQualityService()


def quality(*, status: str = "LIVE") -> dict:
    now = now_shanghai()
    age = {"LIVE": 1, "STALE": 40, "OLD": 90, "UNAVAILABLE": 400}[status]
    return QUALITY_SERVICE.assess(now - timedelta(seconds=age), server_timestamp=now)


def coverage(requested: int, success: int) -> CoverageReport:
    now = now_shanghai()
    return CoverageReport(
        total_securities=requested,
        quotes_requested=requested,
        quotes_success=success,
        quotes_failed=requested - success,
        filtered_mainboard=success // 2,
        fresh_live_count=success,
        unavailable_count=requested - success,
        last_scan_timestamp=now,
        data_age_seconds=0,
        coverage_rate=0,
        coverage_level="PARTIAL",
        status="PARTIAL",
        scan_id="scan-test",
        **QUALITY_SERVICE.assess(now, server_timestamp=now),
    )


@pytest.mark.parametrize(
    ("requested", "success", "expected_rate", "expected_level"),
    [(100, 90, 0.9, "FULL"), (100, 60, 0.6, "BROAD"), (100, 59, 0.59, "PARTIAL")],
)
def test_coverage_rate_and_level_use_requested_quotes(
    requested: int, success: int, expected_rate: float, expected_level: str
) -> None:
    report = coverage(requested, success)
    assert report.coverage_rate == expected_rate
    assert report.coverage_level == expected_level
    assert report.status == expected_level
    assert report.coverage_rate != report.filtered_mainboard / report.total_securities or success == requested


def make_quote(
    code: str,
    *,
    name: str = "样例",
    market: str | None = None,
    price: float | None = 10.0,
    pct_change: float | None = 1.0,
    amount: float | None = 100_000_000,
    suspended: bool = False,
    freshness: str = "LIVE",
) -> Quote:
    resolved_market = market or ("BJ" if code.startswith("8") else ("SH" if code.startswith("6") else "SZ"))
    return Quote(
        code=code,
        name=name,
        market=resolved_market,
        price=price,
        prev_close=10.0,
        open=10.0,
        high=None if price is None else price + 0.1,
        low=None if price is None else price - 0.1,
        pct_change=pct_change,
        change=None if price is None else price - 10,
        volume=1_000_000,
        amount=amount,
        turnover_rate=2.0,
        volume_ratio=1.2,
        amplitude=1.0,
        suspended=suspended,
        **quality(status=freshness),
    )


class CoverageProvider:
    def __init__(self) -> None:
        self.quotes = [
            make_quote("600001"),
            make_quote("300001", freshness="STALE"),
            make_quote("688001", freshness="OLD"),
            make_quote("830001", market="BJ", freshness="UNAVAILABLE"),
            make_quote("600002", name="ST样例"),
            make_quote("600003", suspended=True),
            make_quote("600004", amount=1),
            make_quote("600005", price=11.0, pct_change=5.0),
            make_quote("600006", price=None, pct_change=None),
            make_quote("600007", pct_change=6.0),
            make_quote("200001"),
        ]

    async def get_all_a_shares(self):
        return 12, self.quotes

    async def get_index_quote(self, code, market):
        return make_quote(code, market=market)

    async def get_kline(self, code, period, limit, adjust="qfq", *, quote=None):
        now = now_shanghai()
        bars = [
            Kline(
                timestamp=now - timedelta(days=offset),
                open=9,
                high=11,
                low=8,
                close=10,
                volume=1_000_000,
                amount=100_000_000,
            )
            for offset in range(80, 0, -1)
        ]
        return KlineResult(
            code=code,
            period=period,
            klines=bars,
            **QUALITY_SERVICE.assess(now, server_timestamp=now),
        )

    def metrics_snapshot(self):
        return {key: 0 for key in ("required", "cache_hit", "cache_miss", "network_fetch", "success", "failed", "stale_used", "provisional_used")}

    def health(self):
        blank = {"success_count": 0, "failure_count": 0, "empty_data_count": 0, "timeout_count": 0}
        return {"providers": {"eastmoney": blank, "tencent": blank}}


async def test_scan_records_filter_counts_and_freshness_without_refetching() -> None:
    provider = CoverageProvider()
    scanner = ScannerService(provider, AsyncTTLCache())
    await scanner.scan_mainboard(top_n=30)
    report = await scanner.get_scan_coverage()

    assert report.total_securities == 12
    assert report.quotes_requested == 12
    assert report.quotes_success == 11
    assert report.quotes_failed == 1
    assert report.filtered_mainboard == 1
    assert report.excluded_chinext == 1
    assert report.excluded_star == 1
    assert report.excluded_bse == 1
    assert report.excluded_st == 1
    assert report.excluded_suspended == 1
    assert report.excluded_illiquid == 1
    assert report.excluded_limit_untradable == 1
    assert report.excluded_invalid_quote == 1
    assert report.excluded_pct_change == 1
    assert report.excluded_other == 1
    excluded_total = sum(
        (
            report.excluded_chinext,
            report.excluded_star,
            report.excluded_bse,
            report.excluded_st,
            report.excluded_suspended,
            report.excluded_illiquid,
            report.excluded_limit_untradable,
            report.excluded_invalid_quote,
            report.excluded_pct_change,
            report.excluded_other,
        )
    )
    assert excluded_total + report.filtered_mainboard == report.quotes_success
    assert report.freshness.model_dump() == {"live": 8, "stale": 1, "old": 1, "unavailable": 2}
    assert sum(report.freshness.model_dump().values()) == report.quotes_requested
    assert report.scan_candidates_total == 1
    assert report.scan_top_n == 1


async def test_live_and_coverage_endpoint_publish_the_same_snapshot(monkeypatch) -> None:
    now = now_shanghai()
    common_quality = QUALITY_SERVICE.assess(now, server_timestamp=now)
    report = coverage(100, 95).model_copy(
        update={
            "filtered_mainboard": 52,
            "industry_total": 31,
            "industry_success": 31,
            "concept_total": 185,
            "concept_success": 182,
            "fresh_live_count": 92,
            "fresh_stale_count": 2,
            "fresh_old_count": 1,
            "unavailable_count": 5,
            "failure_sources": {"eastmoney": 3, "tencent": 1, "kline": 0, "sector": 3},
            "missing_fields": {"volume_ratio missing": 4, "turnover_rate missing": 2, "timestamp missing": 1},
        }
    )
    market = MarketOverview(
        indices={"shanghai": IndexSnapshot(code="000001", name="上证", price=3900, pct_change=0.1)},
        breadth=MarketBreadth(
            up_count=60,
            down_count=38,
            flat_count=2,
            limit_up_count=AvailableValue(value=None, available=False),
            limit_down_count=AvailableValue(value=None, available=False),
        ),
        amount=1_000_000,
        **common_quality,
    )
    scan = ScanResult(
        coverage=ScanCoverage(total=100, success=95, filtered_mainboard=52, failed=5, coverage_rate=0.95),
        candidates=[],
        scan_id=report.scan_id,
        **common_quality,
    )
    sector_items = [
        SectorItem(name="板块一", code="BK0001", pct_change=1.2, amount=1000, up_count=8, down_count=2, rank=1)
    ]
    industry = SectorRanking(
        sector_type="industry", items=sector_items, total_count=31, success_count=31, failed_count=0, **common_quality
    )
    concept = SectorRanking(
        sector_type="concept", items=sector_items, total_count=185, success_count=182, failed_count=3, **common_quality
    )

    async def loader():
        return market, scan, {}, report, industry, concept

    cache = LiveSnapshotCache(loader)
    await cache.refresh_once()
    monkeypatch.setattr(routes, "live_cache", cache)
    monkeypatch.setattr(routes, "_web_secret", lambda: "coverage-secret")

    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        coverage_response = await client.get("/gpt/coverage-secret/coverage")
        live_response = await client.get("/gpt/coverage-secret/live/unique")

    assert coverage_response.status_code == 200
    assert coverage_response.json()["data"] == report.model_dump(mode="json")
    assert live_response.status_code == 200
    for expected in ("请求行情", "95", "行业覆盖", "31 / 31", "概念覆盖", "182 / 185", "95.00%", "FULL"):
        assert expected in live_response.text
    assert "行业 Top20" in live_response.text
    assert "概念 Top20" in live_response.text
    assert "volume_ratio missing" in live_response.text
