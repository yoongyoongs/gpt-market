from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx

from app.api import routes
from app.main import api
from app.models import Kline, KlineResult, OpportunityScanResult, Quote
from app.services.data_quality import DataQualityService
from app.services.opportunity_scoring import build_candidate_pool, build_opportunity_candidate
from app.services.scanner import ScannerService
from app.utils.time import SHANGHAI, now_shanghai


QUALITY = DataQualityService()


def quote(code: str = "600001", *, pct_change: float = 1.0, amount: float = 100_000_000, volume_ratio: float = 1.2) -> Quote:
    now = now_shanghai()
    return Quote(
        code=code,
        name="样例股份",
        market="SH",
        price=10.0,
        prev_close=9.9,
        open=9.8,
        high=10.2,
        low=9.6,
        pct_change=pct_change,
        change=0.1,
        volume=10_000_000,
        amount=amount,
        turnover_rate=3.0,
        volume_ratio=volume_ratio,
        amplitude=4.0,
        suspended=False,
        **QUALITY.assess(now, server_timestamp=now),
    )


def rising_day_klines(count: int = 260) -> list[Kline]:
    start = datetime(2025, 1, 1, tzinfo=SHANGHAI)
    rows: list[Kline] = []
    for index in range(count):
        base = 7.0 + index * 0.012
        rows.append(
            Kline(
                timestamp=start + timedelta(days=index),
                open=base,
                high=base + 0.35,
                low=base - 0.25,
                close=base + 0.18,
                volume=1_000_000 + index * 1000,
                amount=100_000_000,
            )
        )
    rows[-1] = rows[-1].model_copy(update={"open": 9.8, "high": 10.2, "low": 9.6, "close": 10.0, "volume": 1_600_000})
    return rows


def declining_week_klines(count: int = 80) -> list[Kline]:
    start = datetime(2024, 1, 1, tzinfo=SHANGHAI)
    return [
        Kline(
            timestamp=start + timedelta(days=index * 7),
            open=20 - index * 0.08,
            high=20.2 - index * 0.08,
            low=19.6 - index * 0.08,
            close=19.8 - index * 0.08,
            volume=1_000_000,
            amount=100_000_000,
        )
        for index in range(count)
    ]


def test_candidate_pool_is_wide_and_multi_channel() -> None:
    quotes = [quote(f"600{index:03d}", pct_change=(index % 11) - 3, amount=50_000_000 + index * 1_000_000) for index in range(500)]
    pool, counts = build_candidate_pool(quotes, target_size=420)
    assert len(pool) == 420
    assert {"trend_improvement_proxy", "low_position_proxy", "flow_activity", "relative_strength", "liquidity_floor"} <= set(counts)
    assert len({item.code for item in pool}) == len(pool)


def test_phase1_opportunity_score_exposes_missing_real_data_and_rr_formula() -> None:
    result = build_opportunity_candidate(quote(), rising_day_klines(), rising_day_klines(80), 0.0, {})
    assert result.score_version == "v2"
    assert result.fundamental_score is None
    assert result.catalyst_score is None
    assert "fundamental" in result.data_quality.missing_fields
    assert "catalyst" in result.data_quality.missing_fields
    assert result.risk_reward_ratio is not None
    assert "risk_reward" in result.score_breakdown
    assert "opportunity_score = clamp" in result.score_formula
    assert result.grade in {"B", "C"}


def test_declining_week_trend_caps_trend_score() -> None:
    result = build_opportunity_candidate(quote(), rising_day_klines(), declining_week_klines(), 0.0, {})
    assert result.week_trend == "DECLINING"
    assert result.trend_score <= 9
    assert result.grade == "C"


class V2Provider:
    def __init__(self) -> None:
        self.quotes = [quote(f"600{index:03d}", pct_change=(index % 7) - 1, amount=80_000_000 + index * 1_000_000) for index in range(360)]

    async def get_all_a_shares(self):
        return len(self.quotes), self.quotes

    async def get_index_quote(self, code, market):
        return quote(code)

    async def get_kline(self, code, period, limit, adjust="qfq", *, quote=None):
        rows = rising_day_klines(limit)
        return KlineResult(code=code, period=period, klines=rows, **QUALITY.assess(rows[-1].timestamp))

    def metrics_snapshot(self):
        return {key: 0 for key in ("required", "cache_hit", "cache_miss", "network_fetch", "success", "failed", "stale_used", "provisional_used")}

    def health(self):
        blank = {"success_count": 0, "failure_count": 0, "empty_data_count": 0, "timeout_count": 0}
        return {"providers": {"eastmoney": blank, "tencent": blank}}


async def test_scan_v2_route_uses_separate_contract(monkeypatch) -> None:
    scanner = ScannerService(V2Provider(), routes.container.cache, concurrency=20)
    fake = SimpleNamespace(scanner=scanner)
    monkeypatch.setattr(routes, "container", fake)
    monkeypatch.setattr(routes, "_web_secret", lambda: "v2-secret")
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/gpt/v2-secret/scan/v2", params={"top_n": 5, "pool_size": 300})
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["score_version"] == "v2"
    assert len(payload["raw_top30"]) == 5
    assert payload["raw_top30"][0]["score_version"] == "v2"
    assert "v1_top30" not in payload
