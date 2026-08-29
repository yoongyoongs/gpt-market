from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Quality = Literal["LIVE", "STALE", "OLD", "UNAVAILABLE", "CONFLICT"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]
TimestampSource = Literal["eastmoney", "tencent", "fetch_time"]


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
    provisional: bool = False


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
    total_count: int | None = None
    success_count: int | None = None
    failed_count: int | None = None


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


class ScoreComponent(MarketModel):
    raw_value: dict[str, object] = Field(default_factory=dict)
    normalized_value: float | dict[str, object] | None = None
    score: float | None
    max_score: float
    reason: list[str] = Field(default_factory=list)
    data_source: list[str] = Field(default_factory=list)
    data_timestamp: datetime | None = None
    coverage: bool


class FundamentalConflict(MarketModel):
    field: str
    selected_source: str
    selected_value: object | None = None
    conflicting_source: str
    conflicting_value: object | None = None


class FundamentalField(MarketModel):
    value: object | None = None
    source: str
    upstream_source: str
    source_type: Literal["official", "vendor", "aggregator"]
    report_period: str | None = None
    fetch_time: datetime
    coverage: bool
    stale: bool
    confidence: Confidence
    error: str | None = None
    conflicts: list[FundamentalConflict] = Field(default_factory=list)


class FundamentalQuarter(MarketModel):
    report_period: str | None = None
    notice_date: datetime | None = None
    revenue: float | None = None
    revenue_yoy: float | None = None
    revenue_qoq: float | None = None
    net_profit: float | None = None
    net_profit_yoy: float | None = None
    net_profit_qoq: float | None = None
    deducted_net_profit: float | None = None
    deducted_net_profit_yoy: float | None = None
    operating_cash_flow: float | None = None
    roe: float | None = None
    gross_margin: float | None = None
    debt_ratio: float | None = None
    source: str = "eastmoney_datacenter"
    upstream_source: str = "eastmoney"
    fetch_time: datetime | None = None
    coverage: float = Field(default=0.0, ge=0, le=1)
    stale: bool = False
    confidence: Confidence = "MEDIUM"
    error: str | None = None


class FundamentalSnapshot(MarketModel):
    code: str
    fields: dict[str, FundamentalField] = Field(default_factory=dict)
    quarterly_trend: list[FundamentalQuarter] = Field(default_factory=list)
    performance_forecast: FundamentalField | None = None
    performance_express: FundamentalField | None = None
    audit_opinion: FundamentalField | None = None
    report_period: str | None = None
    fetch_time: datetime
    source: str
    upstream_sources: list[str] = Field(default_factory=list)
    coverage: float = Field(default=0.0, ge=0, le=1)
    conflicts: list[FundamentalConflict] = Field(default_factory=list)
    stale: bool = False
    confidence: Confidence = "LOW"
    error: str | None = None

    def with_coverage(self) -> "FundamentalSnapshot":
        required = (
            "revenue", "revenue_yoy", "revenue_qoq", "net_profit", "deducted_net_profit",
            "net_profit_yoy", "net_profit_qoq", "roe", "operating_cash_flow", "gross_margin",
            "debt_ratio", "pe", "pb",
        )
        covered = sum(bool(self.fields.get(name) and self.fields[name].coverage) for name in required)
        rate = covered / len(required)
        confidence: Confidence = "HIGH" if rate >= 0.85 and not self.conflicts else ("MEDIUM" if rate >= 0.5 else "LOW")
        return self.model_copy(update={"coverage": round(rate, 4), "confidence": confidence})


class DataCoverage(MarketModel):
    quote: bool = False
    day_kline: bool = False
    week_kline: bool = False
    position: bool = False
    trend: bool = False
    flow: bool = False
    risk_reward: bool = False
    liquidity: bool = False
    fundamental: bool = False
    catalyst: bool = False


class DataQualityReport(MarketModel):
    data_quality: Quality
    coverage: DataCoverage
    stale_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    conflict_fields: list[str] = Field(default_factory=list)
    provider_status: dict[str, object] = Field(default_factory=dict)


class SupportResistance(MarketModel):
    support: float | None = None
    resistance: float | None = None
    stop_loss: float | None = None
    target_1: float | None = None
    target_2: float | None = None
    downside_pct: float | None = None
    upside_pct: float | None = None
    risk_reward_ratio: float | None = None


class OpportunityCandidate(MarketModel):
    stock_code: str
    stock_name: str
    price: float
    pct_change: float
    amount: float | None = None
    opportunity_score: float
    position_score: float
    fundamental_score: float | None = None
    trend_score: float
    flow_score: float
    catalyst_score: float | None = None
    risk_reward_score: float
    liquidity_score: float
    risk_penalty: float
    fundamental_risk_penalty: float = 0.0
    grade: Literal["A", "B", "C"]
    support: float | None = None
    resistance: float | None = None
    stop_loss: float | None = None
    target_1: float | None = None
    target_2: float | None = None
    downside_pct: float | None = None
    upside_pct: float | None = None
    risk_reward_ratio: float | None = None
    week_trend: str
    day_trend: str
    data_coverage: DataCoverage
    data_quality: DataQualityReport
    score_version: Literal["v2"] = "v2"
    entry_score: float | None = None
    score_breakdown: dict[str, ScoreComponent]
    score_formula: str
    raw_inputs: dict[str, object] = Field(default_factory=dict)
    reason: list[str] = Field(default_factory=list)
    hard_reject: bool = False
    snapshot_id: str


class OpportunityScanResult(Freshness):
    coverage: ScanCoverage
    raw_top30: list[OpportunityCandidate]
    action_top30: list[OpportunityCandidate]
    top100: list[OpportunityCandidate]
    candidate_pool_size: int
    channel_counts: dict[str, int] = Field(default_factory=dict)
    scan_id: str
    score_version: Literal["v2"] = "v2"
    score_formula: str
    missing_data_sources: list[str] = Field(default_factory=list)
    duration_seconds: float | None = None


class FreshnessDistribution(MarketModel):
    live: int = 0
    stale: int = 0
    old: int = 0
    unavailable: int = 0


class CoverageReport(Freshness):
    total_securities: int
    quotes_requested: int
    quotes_success: int
    quotes_failed: int
    filtered_mainboard: int
    excluded_chinext: int = 0
    excluded_star: int = 0
    excluded_bse: int = 0
    excluded_st: int = 0
    excluded_suspended: int = 0
    excluded_illiquid: int = 0
    excluded_limit_untradable: int = 0
    excluded_invalid_quote: int = 0
    excluded_pct_change: int = 0
    excluded_other: int = 0
    industry_total: int | None = None
    industry_success: int | None = None
    concept_total: int | None = None
    concept_success: int | None = None
    scan_candidates_total: int = 0
    scan_top_n: int = 0
    fresh_live_count: int = 0
    fresh_stale_count: int = 0
    fresh_old_count: int = 0
    unavailable_count: int = 0
    freshness: FreshnessDistribution = Field(default_factory=FreshnessDistribution)
    failure_sources: dict[str, int] = Field(default_factory=dict)
    missing_fields: dict[str, int] = Field(default_factory=dict)
    fund_total: int | None = None
    fund_quote_success: int | None = None
    fund_fee_verified: int | None = None
    etf_total: int | None = None
    lof_total: int | None = None
    reit_total: int | None = None
    last_scan_timestamp: datetime | None
    data_age_seconds: float
    coverage_rate: float
    coverage_level: Literal["FULL", "BROAD", "PARTIAL"]
    status: Literal["FULL", "BROAD", "PARTIAL"]
    scan_id: str

    @model_validator(mode="after")
    def normalize_coverage(self) -> "CoverageReport":
        rate = self.quotes_success / self.quotes_requested if self.quotes_requested else 0.0
        level = "FULL" if rate >= 0.9 else ("BROAD" if rate >= 0.6 else "PARTIAL")
        self.coverage_rate = round(rate, 4)
        self.coverage_level = level
        self.status = level
        self.freshness = FreshnessDistribution(
            live=self.fresh_live_count,
            stale=self.fresh_stale_count,
            old=self.fresh_old_count,
            unavailable=self.unavailable_count,
        )
        return self


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
OpportunityScanResponse = OpportunityScanResult
