from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.api import routes
from app.main import api
from app.mcp import server as mcp_server
from app.models import (
    AvailableValue,
    IndexSnapshot,
    Kline,
    MarketBreadth,
    MarketOverview,
    Quote,
    ScanCandidate,
    ScanCoverage,
    ScanResult,
    SectorItem,
    SectorRanking,
    StockDetail,
    TechnicalIndicators,
)
from app.services.data_quality import DataQualityService
from app.utils.time import SHANGHAI


SOURCE_TIME = datetime(2026, 8, 27, 14, 40, 1, tzinfo=SHANGHAI)
SERVER_TIME = SOURCE_TIME + timedelta(seconds=1)
QUALITY = DataQualityService().assess(SOURCE_TIME, server_timestamp=SERVER_TIME)


def make_quote(code: str, price: float) -> Quote:
    return Quote(
        code=code,
        name={"002284": "亚太股份", "600722": "金牛化工", "600519": "贵州茅台"}[code],
        market="SZ" if code.startswith("0") else "SH",
        price=price,
        prev_close=round(price - 0.1, 2),
        open=round(price - 0.05, 2),
        high=round(price + 0.1, 2),
        low=round(price - 0.2, 2),
        pct_change=1.01,
        change=0.1,
        volume=1_000_000,
        amount=88_000_000.0,
        turnover_rate=2.2,
        volume_ratio=1.3,
        amplitude=3.0,
        **QUALITY,
    )


QUOTES = {
    "002284": make_quote("002284", 9.98),
    "600722": make_quote("600722", 5.12),
    "600519": make_quote("600519", 1488.0),
}
KLINES = [
    Kline(
        timestamp=SOURCE_TIME - timedelta(days=offset),
        open=9.0,
        high=10.2,
        low=8.8,
        close=9.8,
        volume=1_000_000,
        amount=88_000_000,
    )
    for offset in range(3, 0, -1)
]
DETAIL = StockDetail(
    quote=QUOTES["002284"],
    technical=TechnicalIndicators(ma5=9.6, ma20=9.3, ma60=8.9, atr14=0.31, rsi14=56.2, high_20d=10.22, low_20d=8.72),
    day_klines=KLINES,
    minute_5_klines=KLINES,
    **QUALITY,
)
MARKET = MarketOverview(
    indices={"shanghai": IndexSnapshot(code="000001", name="上证指数", price=3900.12, pct_change=0.32)},
    breadth=MarketBreadth(
        up_count=3200,
        down_count=1700,
        flat_count=180,
        limit_up_count=AvailableValue(value=None, available=False),
        limit_down_count=AvailableValue(value=None, available=False),
    ),
    amount=862_000_000_000,
    **QUALITY,
)
SECTORS = SectorRanking(
    sector_type="industry",
    items=[SectorItem(name=f"行业{i}", code=f"BK{i:04d}", pct_change=3 - i / 10, amount=1_000_000, up_count=10, down_count=2, rank=i) for i in range(1, 11)],
    **QUALITY,
)
SCAN = ScanResult(
    coverage=ScanCoverage(total=5900, success=5890, filtered_mainboard=2160, failed=10, coverage_rate=0.9983),
    candidates=[
        ScanCandidate(
            code="002284",
            name="亚太股份",
            price=9.98,
            pct_change=1.01,
            amount=88_000_000,
            turnover_rate=2.2,
            volume_ratio=1.3,
            ma5=9.6,
            ma20=9.3,
            ma60=8.9,
            trend_score=25,
            volume_score=15,
            relative_strength_score=15,
            position_score=18,
            liquidity_score=12,
            total_score=85,
            reason=["同一评分结果"],
            snapshot_id=QUALITY["snapshot_id"],
        )
    ],
    scan_id=DataQualityService.scan_id(SOURCE_TIME),
    **QUALITY,
)


@pytest.fixture
def parity_container(monkeypatch):
    async def get_quote(code: str):
        return QUOTES[code]

    async def get_detail(code: str):
        assert code == "002284"
        return DETAIL

    async def get_market():
        return MARKET

    async def get_sectors(sector_type: str = "industry", limit: int = 30):
        assert sector_type == "industry"
        return SECTORS.model_copy(update={"items": SECTORS.items[:limit]})

    async def scan(*args, **kwargs):
        return SCAN

    fake = SimpleNamespace(
        quotes=SimpleNamespace(get_quote=get_quote),
        klines=SimpleNamespace(get_stock_detail=get_detail),
        market=SimpleNamespace(get_market_overview=get_market),
        sectors=SimpleNamespace(get_sector_ranking=get_sectors),
        scanner=SimpleNamespace(scan_mainboard=scan),
    )
    monkeypatch.setattr(routes, "container", fake)
    monkeypatch.setattr(mcp_server, "container", fake)
    monkeypatch.setattr(routes, "_web_secret", lambda: "parity-secret")
    return fake


@pytest.mark.parametrize("code", ["002284", "600722", "600519"])
async def test_quote_mcp_web_parity(parity_container, code: str) -> None:
    mcp_data = await mcp_server.get_quote(code)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/gpt/parity-secret/stock/{code}")
    assert response.status_code == 200
    assert response.json()["data"] == mcp_data
    for field in ("price", "pct_change", "amount", "turnover_rate", "volume_ratio", "source_timestamp", "snapshot_id"):
        assert response.json()["data"][field] == mcp_data[field]


async def test_detail_market_sector_and_scan_mcp_web_parity(parity_container) -> None:
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        detail_web = (await client.get("/gpt/parity-secret/stock/002284/detail")).json()["data"]
        market_web = (await client.get("/gpt/parity-secret/market")).json()["data"]
        sectors_web = (await client.get("/gpt/parity-secret/sectors", params={"sector_type": "industry", "limit": 10})).json()["data"]
        scan_web = (await client.get("/gpt/parity-secret/scan", params={"top_n": 10})).json()["data"]

    detail_mcp = await mcp_server.get_stock_detail("002284")
    market_mcp = await mcp_server.get_market_overview()
    sectors_mcp = await mcp_server.get_sector_ranking("industry", 10)
    scan_mcp = await mcp_server.scan_mainboard(top_n=10)

    assert detail_web == detail_mcp
    assert market_web == market_mcp
    assert sectors_web == sectors_mcp
    assert scan_web == scan_mcp
    assert detail_web["technical"]["ma20"] == detail_mcp["technical"]["ma20"]
    assert detail_web["technical"]["atr14"] == detail_mcp["technical"]["atr14"]
    assert scan_web["coverage"] == scan_mcp["coverage"]
    assert scan_web["scan_id"] == scan_mcp["scan_id"]


async def test_gpt_web_secret_is_required(parity_container) -> None:
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/gpt/wrong/stock/002284")
    assert response.status_code == 404
