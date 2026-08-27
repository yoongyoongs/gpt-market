from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Quality = Literal["LIVE", "STALE", "OLD", "UNAVAILABLE", "CONFLICT"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]
TimestampSource = Literal["eastmoney", "fetch_time"]


class MarketModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Freshness(MarketModel):
    source: str = "eastmoney"
    source_timestamp: datetime
    data_timestamp: datetime
    server_timestamp: datetime
    age_seconds: float = Field(ge=0)
    stale: bool
    quality: Quality
    timestamp_source: TimestampSource
    snapshot_id: str
    confidence: Confidence


class Quote(Freshness):
    code: str
    name: str
    market: Literal["SH", "SZ", "BJ"]
    price: float | None
    prev_close: float | None
    open: float | None
    high: float | None
    low: float | None
    pct_change: float | None
    change: float | None
    volume: int | None
    amount: float | None
    turnover_rate: float | None
    volume_ratio: float | None
    amplitude: float | None
    suspended: bool = False


class Kline(MarketModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float


class KlineResult(Freshness):
    code: str
    period: str
    klines: list[Kline]


class TechnicalIndicators(MarketModel):
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    atr14: float | None = None
    rsi14: float | None = None
    high_20d: float | None = None
    low_20d: float | None = None
    high_60d: float | None = None
    low_60d: float | None = None
    distance_ma20_pct: float | None = None
    distance_high_20d_pct: float | None = None
    return_5d: float | None = None
    return_20d: float | None = None


class StockDetail(Freshness):
    quote: Quote
    technical: TechnicalIndicators
    day_klines: list[Kline]
    minute_5_klines: list[Kline]


class AvailableValue(MarketModel):
    value: int | float | None
    available: bool


class IndexSnapshot(MarketModel):
    code: str
    name: str
    price: float | None
    pct_change: float | None


class MarketBreadth(MarketModel):
    up_count: int
    down_count: int
    flat_count: int
    limit_up_count: AvailableValue
    limit_down_count: AvailableValue


class MarketOverview(Freshness):
    indices: dict[str, IndexSnapshot]
    breadth: MarketBreadth
    amount: float


class SectorItem(MarketModel):
    name: str
    code: str
    pct_change: float | None
    amount: float | None
    up_count: int | None
    down_count: int | None
    limit_up_count: int | None = None
    rank: int
    rank_10m_ago: int | None = None
    rank_30m_ago: int | None = None
    rank_change_10m: int | None = None
    rank_change_30m: int | None = None


class SectorRanking(Freshness):
    sector_type: Literal["industry", "concept"]
    items: list[SectorItem]


class ScanCandidate(MarketModel):
    code: str
    name: str
    price: float
    pct_change: float
    amount: float
    turnover_rate: float | None
    volume_ratio: float | None
    ma5: float | None
    ma20: float | None
    ma60: float | None
    trend_score: float
    volume_score: float
    relative_strength_score: float
    position_score: float
    liquidity_score: float
    total_score: float
    reason: list[str]
    snapshot_id: str


class CoverageReport(Freshness):
    total_securities: int
    quotes_success: int
    quotes_failed: int
    filtered_mainboard: int
    last_scan_timestamp: datetime | None
    data_age_seconds: float
    coverage_rate: float
    status: Literal["FULL", "BROAD", "PARTIAL"]
    scan_id: str


class ScanCoverage(MarketModel):
    total: int
    success: int
    filtered_mainboard: int
    failed: int
    coverage_rate: float


class ScanResult(Freshness):
    coverage: ScanCoverage
    candidates: list[ScanCandidate]
    scan_id: str


class ErrorResponse(MarketModel):
    ok: Literal[False] = False
    error: str
    source: str = "eastmoney"
    server_timestamp: datetime


# Public semantic schemas shared by both transport adapters.  The aliases keep the
# original domain names stable while documenting the response contract explicitly.
QuoteResponse = Quote
StockDetailResponse = StockDetail
MarketOverviewResponse = MarketOverview
SectorRankingResponse = SectorRanking
ScanResponse = ScanResult
CoverageResponse = CoverageReport
