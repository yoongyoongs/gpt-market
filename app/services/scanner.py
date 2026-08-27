from __future__ import annotations

import asyncio
import math
from datetime import datetime

from app.cache import AsyncTTLCache
from app.models import CoverageReport, Quote, ScanCandidate, ScanCoverage, ScanResult, TechnicalIndicators
from app.providers.base import MarketDataProvider
from app.services.data_quality import DataQualityService
from app.services.technical_indicator_service import TechnicalIndicatorService
from app.utils.time import now_shanghai

MAINBOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")


def is_st(name: str) -> bool:
    normalized = name.upper().replace(" ", "")
    return normalized.startswith("ST") or normalized.startswith("*ST") or "退" in normalized


def is_mainboard(code: str) -> bool:
    return code.startswith(MAINBOARD_PREFIXES)


def at_price_limit(quote: Quote, direction: str) -> bool:
    if quote.price is None or quote.prev_close is None or quote.prev_close <= 0:
        return False
    ratio = 0.05 if is_st(quote.name) else 0.10
    target = round(quote.prev_close * (1 + ratio if direction == "up" else 1 - ratio) + 1e-8, 2)
    return quote.price >= target - 0.001 if direction == "up" else quote.price <= target + 0.001


def is_one_price_board(quote: Quote) -> bool:
    return quote.high is not None and quote.low is not None and quote.high == quote.low and (quote.volume or 0) > 0


def coverage_status(rate: float) -> str:
    return "FULL" if rate >= 0.9 else ("BROAD" if rate >= 0.6 else "PARTIAL")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def scan_cache_key(
    top_n: int,
    max_pct_change: float,
    min_amount: float,
    exclude_st: bool,
    exclude_limit_up: bool,
    exclude_limit_down: bool,
) -> str:
    """Canonicalize equivalent MCP/Web input types into one shared cache key."""
    return (
        f"scan:mainboard:{int(top_n)}:{float(max_pct_change):.8g}:{float(min_amount):.8g}:"
        f"{bool(exclude_st)}:{bool(exclude_limit_up)}:{bool(exclude_limit_down)}"
    )


def score_candidate(quote: Quote, technical: TechnicalIndicators, benchmark_pct: float = 0.0, recent_three: list[float] | None = None) -> ScanCandidate:
    assert quote.price is not None and quote.pct_change is not None and quote.amount is not None
    trend = 0.0
    reasons: list[str] = []
    if technical.ma20 and quote.price > technical.ma20:
        trend += 10
        reasons.append("价格位于MA20上方")
    if technical.ma5 and technical.ma20 and technical.ma5 > technical.ma20:
        trend += 8
    if technical.ma20 and technical.ma60 and technical.ma20 > technical.ma60:
        trend += 7

    ratio = quote.volume_ratio or 0
    volume = _clamp(ratio / 2 * 10, 0, 10)
    volume += _clamp((math.log10(max(quote.amount, 1)) - 7) / 2 * 10, 0, 10)
    if quote.amount >= 100_000_000:
        reasons.append("成交额充足")

    excess = quote.pct_change - benchmark_pct
    relative = _clamp(10 + excess * 2.5, 0, 20)
    if excess > 0.5:
        reasons.append("相对指数走强")

    pct = quote.pct_change
    position = 8 if 0 <= pct <= 3 else (6 if -2 <= pct < 0 else (3 if 3 < pct <= 5 else 0))
    if 0 <= pct <= 3:
        reasons.append("涨幅尚未过高")
    distance_ma = technical.distance_ma20_pct
    if distance_ma is not None:
        position += 7 if -3 <= distance_ma <= 5 else (4 if -6 <= distance_ma <= 10 else 0)
    distance_high = technical.distance_high_20d_pct
    if distance_high is not None:
        position += 5 if -15 <= distance_high <= -3 else (3 if distance_high < -15 else 1)
    if recent_three and all(value >= 4 for value in recent_three):
        position -= 4
    position = _clamp(position, 0, 20)

    liquidity = _clamp((math.log10(max(quote.amount, 1)) - 7) / 2 * 8, 0, 8)
    turnover = quote.turnover_rate or 0
    liquidity += 4 if 1 <= turnover <= 12 else (2 if 0.3 <= turnover <= 20 else 0)
    liquidity += 0 if is_one_price_board(quote) else 3
    liquidity = _clamp(liquidity, 0, 15)
    total = trend + volume + relative + position + liquidity
    return ScanCandidate(
        code=quote.code, name=quote.name, price=quote.price, pct_change=pct, amount=quote.amount,
        turnover_rate=quote.turnover_rate, volume_ratio=quote.volume_ratio,
        ma5=technical.ma5, ma20=technical.ma20, ma60=technical.ma60,
        trend_score=round(trend, 2), volume_score=round(volume, 2),
        relative_strength_score=round(relative, 2), position_score=round(position, 2),
        liquidity_score=round(liquidity, 2), total_score=round(total, 2), reason=reasons,
        snapshot_id=quote.snapshot_id,
    )


class ScannerService:
    def __init__(
        self,
        provider: MarketDataProvider,
        cache: AsyncTTLCache,
        concurrency: int = 12,
        indicators: TechnicalIndicatorService | None = None,
        quality: DataQualityService | None = None,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.concurrency = concurrency
        self.indicators = indicators or TechnicalIndicatorService()
        self.quality = quality or DataQualityService()
        self.last_result: ScanResult | None = None

    async def scan_mainboard(
        self, top_n: int = 30, max_pct_change: float = 5.0, min_amount: float = 50_000_000,
        exclude_st: bool = True, exclude_limit_up: bool = True, exclude_limit_down: bool = True,
    ) -> ScanResult:
        if not 1 <= top_n <= 100:
            raise ValueError("top_n must be between 1 and 100")
        cache_key = scan_cache_key(
            top_n, max_pct_change, min_amount, exclude_st, exclude_limit_up, exclude_limit_down
        )

        async def load() -> ScanResult:
            market_data, sh_index, sz_index = await asyncio.gather(
                self.provider.get_all_a_shares(),
                self.provider.get_index_quote("000001", "SH"),
                self.provider.get_index_quote("399001", "SZ"),
                return_exceptions=True,
            )
            if isinstance(market_data, Exception):
                raise market_data
            total, quotes = market_data
            benchmarks = {
                "SH": 0.0 if isinstance(sh_index, Exception) else (sh_index.pct_change or 0.0),
                "SZ": 0.0 if isinstance(sz_index, Exception) else (sz_index.pct_change or 0.0),
            }
            eligible: list[Quote] = []
            for quote in quotes:
                if not is_mainboard(quote.code) or quote.suspended or quote.price is None or quote.pct_change is None:
                    continue
                if exclude_st and is_st(quote.name):
                    continue
                if (quote.amount or 0) < min_amount or quote.pct_change > max_pct_change:
                    continue
                if exclude_limit_up and at_price_limit(quote, "up"):
                    continue
                if exclude_limit_down and at_price_limit(quote, "down"):
                    continue
                if is_one_price_board(quote):
                    continue
                eligible.append(quote)

            def preliminary(item: Quote) -> float:
                pct_fit = 10 - abs((item.pct_change or 0) - 1.5) * 2
                ratio = min(item.volume_ratio or 0, 3) * 3
                liquidity = max(0, math.log10(max(item.amount or 1, 1)) - 7) * 2
                return pct_fit + ratio + liquidity

            shortlist = sorted(eligible, key=preliminary, reverse=True)[: min(max(top_n * 3, 60), 120)]
            semaphore = asyncio.Semaphore(self.concurrency)

            async def enrich(quote: Quote) -> ScanCandidate | None:
                try:
                    async with semaphore:
                        result = await self.provider.get_kline(quote.code, "day", 80)
                    technical = self.indicators.calculate(result.klines, quote.price)
                    recent = [
                        (result.klines[i].close / result.klines[i - 1].close - 1) * 100
                        for i in range(max(1, len(result.klines) - 3), len(result.klines))
                        if result.klines[i - 1].close
                    ]
                    return score_candidate(quote, technical, benchmarks.get(quote.market, 0.0), recent)
                except Exception:
                    fallback = score_candidate(quote, TechnicalIndicators(), benchmarks.get(quote.market, 0.0), [])
                    fallback.reason.append("K线不可用，趋势指标未计分")
                    return fallback

            enriched = await asyncio.gather(*(enrich(item) for item in shortlist))
            candidates = sorted((item for item in enriched if item is not None), key=lambda item: item.total_score, reverse=True)[:top_n]
            success = len(quotes)
            rate = success / total if total else 0.0
            data_ts = max((quote.data_timestamp for quote in quotes), default=now_shanghai())
            quality = self.quality.assess(data_ts, complete=bool(quotes))
            result = ScanResult(
                coverage=ScanCoverage(total=total, success=success, filtered_mainboard=len(eligible), failed=max(0, total - success), coverage_rate=round(rate, 4)),
                candidates=candidates,
                scan_id=self.quality.scan_id(data_ts),
                **quality,
            )
            self.last_result = result
            return result

        return await self.cache.get_or_set(cache_key, 15, load)

    async def get_scan_coverage(self) -> CoverageReport:
        if self.last_result is None:
            await self.scan_mainboard(top_n=1)
        assert self.last_result is not None
        result = self.last_result
        rate = result.coverage.coverage_rate
        current_freshness = self.quality.assess(
            result.source_timestamp,
            timestamp_source=result.timestamp_source,
            complete=result.coverage.success > 0,
        )
        return CoverageReport(
            total_securities=result.coverage.total,
            quotes_success=result.coverage.success,
            quotes_failed=result.coverage.failed,
            filtered_mainboard=result.coverage.filtered_mainboard,
            last_scan_timestamp=result.server_timestamp,
            data_age_seconds=current_freshness["age_seconds"],
            coverage_rate=rate,
            status=coverage_status(rate),
            scan_id=result.scan_id,
            **current_freshness,
        )
