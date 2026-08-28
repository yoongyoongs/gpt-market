from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter
from datetime import datetime
from time import perf_counter

from app.cache import AsyncTTLCache
from app.history import save_scan_snapshot
from app.models import CoverageReport, OpportunityScanResult, Quote, ScanCandidate, ScanCoverage, ScanResult, TechnicalIndicators
from app.providers.base import MarketDataProvider
from app.services.data_quality import DataQualityService
from app.services.opportunity_scoring import Benchmarks, OPPORTUNITY_FORMULA, build_candidate_pool, build_opportunity_candidate
from app.services.technical_indicator_service import TechnicalIndicatorService
from app.utils.time import now_shanghai
from app.services.market_data_service import metric_delta

MAINBOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
logger = logging.getLogger("uvicorn.error")


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


def scan_v2_cache_key(
    top_n: int,
    pool_size: int,
    min_amount: float,
    exclude_st: bool,
    exclude_limit_up: bool,
    exclude_limit_down: bool,
) -> str:
    return (
        f"scan:mainboard:v2:{int(top_n)}:{int(pool_size)}:{float(min_amount):.8g}:"
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
        self.last_coverage: CoverageReport | None = None
        self._coverage_by_scan_id: dict[str, CoverageReport] = {}
        self.last_summary: dict | None = None

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
            scan_started = perf_counter()
            metrics_before = self.provider.metrics_snapshot() if hasattr(self.provider, "metrics_snapshot") else {}
            health_before = self.provider.health() if hasattr(self.provider, "health") else {}
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
            excluded: Counter[str] = Counter()
            for quote in quotes:
                if quote.code.startswith(("300", "301")):
                    excluded["chinext"] += 1
                    continue
                if quote.code.startswith(("688", "689")):
                    excluded["star"] += 1
                    continue
                if quote.market == "BJ" or quote.code.startswith(("4", "8", "92")):
                    excluded["bse"] += 1
                    continue
                if not is_mainboard(quote.code):
                    excluded["other"] += 1
                    continue
                if quote.suspended:
                    excluded["suspended"] += 1
                    continue
                if quote.price is None or quote.pct_change is None:
                    excluded["invalid_quote"] += 1
                    continue
                if exclude_st and is_st(quote.name):
                    excluded["st"] += 1
                    continue
                if (quote.amount or 0) < min_amount:
                    excluded["illiquid"] += 1
                    continue
                if quote.pct_change > max_pct_change:
                    excluded["pct_change"] += 1
                    continue
                if exclude_limit_up and at_price_limit(quote, "up"):
                    excluded["limit_untradable"] += 1
                    continue
                if exclude_limit_down and at_price_limit(quote, "down"):
                    excluded["limit_untradable"] += 1
                    continue
                if is_one_price_board(quote):
                    excluded["limit_untradable"] += 1
                    continue
                eligible.append(quote)

            def preliminary(item: Quote) -> float:
                pct_fit = 10 - abs((item.pct_change or 0) - 1.5) * 2
                ratio = min(item.volume_ratio or 0, 3) * 3
                liquidity = max(0, math.log10(max(item.amount or 1, 1)) - 7) * 2
                return pct_fit + ratio + liquidity

            shortlist = sorted(eligible, key=preliminary, reverse=True)[: min(max(top_n * 3, 60), 120)]
            semaphore = asyncio.Semaphore(self.concurrency)
            kline_errors: Counter[str] = Counter()
            kline_sources: Counter[str] = Counter()
            technical_available: dict[str, tuple[bool, bool, bool]] = {}

            async def enrich(quote: Quote) -> ScanCandidate | None:
                try:
                    async with semaphore:
                        result = await self.provider.get_kline(quote.code, "day", 80, "qfq", quote=quote)
                    kline_sources[result.source] += 1
                    technical = self.indicators.calculate(result.klines, quote.price)
                    technical_available[quote.code] = (
                        technical.ma5 is not None,
                        technical.ma10 is not None,
                        technical.ma20 is not None,
                    )
                    recent = [
                        (result.klines[i].close / result.klines[i - 1].close - 1) * 100
                        for i in range(max(1, len(result.klines) - 3), len(result.klines))
                        if result.klines[i - 1].close
                    ]
                    return score_candidate(quote, technical, benchmarks.get(quote.market, 0.0), recent)
                except Exception as exc:
                    key = f"{type(exc).__name__}:{str(exc)[:160]}"
                    kline_errors[key] += 1
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
            metrics_after = self.provider.metrics_snapshot() if hasattr(self.provider, "metrics_snapshot") else {}
            metrics = metric_delta(metrics_after, metrics_before) if metrics_after else {}
            health_after = self.provider.health() if hasattr(self.provider, "health") else {}

            def provider_delta(name: str, field: str) -> int:
                before = ((health_before.get("providers") or {}).get(name) or {}).get(field, 0)
                after = ((health_after.get("providers") or {}).get(name) or {}).get(field, 0)
                return int(after - before)

            freshness_counts = Counter(quote.quality for quote in quotes)
            missing_fields = {
                "volume_ratio missing": sum(quote.volume_ratio is None for quote in quotes),
                "turnover_rate missing": sum(quote.turnover_rate is None for quote in quotes),
                "timestamp missing": sum(quote.timestamp_source == "fetch_time" for quote in quotes),
            }
            eastmoney_failures = provider_delta("eastmoney", "failure_count")
            tencent_failures = provider_delta("tencent", "failure_count")
            quote_failures = max(0, total - success)
            unavailable = quote_failures + freshness_counts["UNAVAILABLE"] + freshness_counts["CONFLICT"]
            self.last_coverage = CoverageReport(
                total_securities=total,
                quotes_requested=total,
                quotes_success=success,
                quotes_failed=quote_failures,
                filtered_mainboard=len(eligible),
                excluded_chinext=excluded["chinext"],
                excluded_star=excluded["star"],
                excluded_bse=excluded["bse"],
                excluded_st=excluded["st"],
                excluded_suspended=excluded["suspended"],
                excluded_illiquid=excluded["illiquid"],
                excluded_limit_untradable=excluded["limit_untradable"],
                excluded_invalid_quote=excluded["invalid_quote"],
                excluded_pct_change=excluded["pct_change"],
                excluded_other=excluded["other"],
                scan_candidates_total=len(shortlist),
                scan_top_n=len(candidates),
                fresh_live_count=freshness_counts["LIVE"],
                fresh_stale_count=freshness_counts["STALE"],
                fresh_old_count=freshness_counts["OLD"],
                unavailable_count=unavailable,
                failure_sources={
                    "eastmoney": eastmoney_failures,
                    "tencent": tencent_failures,
                    "kline": sum(kline_errors.values()),
                    "sector": 0,
                },
                missing_fields=missing_fields,
                last_scan_timestamp=result.server_timestamp,
                data_age_seconds=result.age_seconds,
                coverage_rate=rate,
                coverage_level=coverage_status(rate),
                status=coverage_status(rate),
                scan_id=result.scan_id,
                **quality,
            )
            self._coverage_by_scan_id[result.scan_id] = self.last_coverage
            if len(self._coverage_by_scan_id) > 100:
                self._coverage_by_scan_id.pop(next(iter(self._coverage_by_scan_id)))

            self.last_summary = {
                "stocks_total": total,
                "quotes_requested": total,
                "quotes_success": success,
                "quotes_failed": max(0, total - success),
                "filtered_mainboard": len(eligible),
                "excluded": dict(excluded),
                "kline_required": len(shortlist),
                "kline_cache_hit": metrics.get("cache_hit", 0),
                "kline_network_fetch": metrics.get("network_fetch", 0),
                "kline_success": metrics.get("success", 0),
                "kline_failed": metrics.get("failed", 0),
                "kline_stale_used": metrics.get("stale_used", 0),
                "eastmoney_success": provider_delta("eastmoney", "success_count"),
                "eastmoney_failure": provider_delta("eastmoney", "failure_count"),
                "eastmoney_empty": provider_delta("eastmoney", "empty_data_count"),
                "eastmoney_timeout": provider_delta("eastmoney", "timeout_count"),
                "tencent_success": provider_delta("tencent", "success_count"),
                "tencent_failure": provider_delta("tencent", "failure_count"),
                "kline_availability_rate": round((len(shortlist) - sum(kline_errors.values())) / len(shortlist), 4) if shortlist else 1.0,
                "cache_hit_rate": round(metrics.get("cache_hit", 0) / len(shortlist), 4) if shortlist else 1.0,
                "ma5_available_top": sum(technical_available.get(item.code, (False, False, False))[0] for item in candidates),
                "ma10_available_top": sum(technical_available.get(item.code, (False, False, False))[1] for item in candidates),
                "ma20_available_top": sum(technical_available.get(item.code, (False, False, False))[2] for item in candidates),
                "scan_duration_seconds": round(perf_counter() - scan_started, 3),
                "kline_errors": dict(kline_errors.most_common(10)),
                "kline_sources": dict(kline_sources.most_common()),
            }
            logger.info("SCAN SUMMARY %s", json.dumps(self.last_summary, ensure_ascii=False, separators=(",", ":")))
            try:
                await save_scan_snapshot(
                    "v1",
                    result,
                    {
                        "top_n": top_n,
                        "max_pct_change": max_pct_change,
                        "min_amount": min_amount,
                        "exclude_st": exclude_st,
                        "exclude_limit_up": exclude_limit_up,
                        "exclude_limit_down": exclude_limit_down,
                    },
                )
            except Exception as exc:
                logger.warning("scan history save failed version=v1 error=%s", exc)
            return result

        return await self.cache.get_or_set(cache_key, 15, load)

    async def scan_mainboard_v2(
        self,
        top_n: int = 30,
        pool_size: int = 420,
        min_amount: float = 50_000_000,
        exclude_st: bool = True,
        exclude_limit_up: bool = True,
        exclude_limit_down: bool = True,
    ) -> OpportunityScanResult:
        if not 1 <= top_n <= 100:
            raise ValueError("top_n must be between 1 and 100")
        if not 300 <= pool_size <= 500:
            raise ValueError("pool_size must be between 300 and 500")
        cache_key = scan_v2_cache_key(top_n, pool_size, min_amount, exclude_st, exclude_limit_up, exclude_limit_down)

        async def load() -> OpportunityScanResult:
            scan_started = perf_counter()
            metrics_before = self.provider.metrics_snapshot() if hasattr(self.provider, "metrics_snapshot") else {}
            health_before = self.provider.health() if hasattr(self.provider, "health") else {}
            market_data, sh_index, sz_index = await asyncio.gather(
                self.provider.get_all_a_shares(),
                self.provider.get_index_quote("000001", "SH"),
                self.provider.get_index_quote("399001", "SZ"),
                return_exceptions=True,
            )
            if isinstance(market_data, Exception):
                raise market_data
            total, quotes = market_data
            benchmarks = Benchmarks(
                sh_pct=0.0 if isinstance(sh_index, Exception) else (sh_index.pct_change or 0.0),
                sz_pct=0.0 if isinstance(sz_index, Exception) else (sz_index.pct_change or 0.0),
            )
            eligible: list[Quote] = []
            excluded: Counter[str] = Counter()
            for quote in quotes:
                if quote.code.startswith(("300", "301")):
                    excluded["chinext"] += 1
                    continue
                if quote.code.startswith(("688", "689")):
                    excluded["star"] += 1
                    continue
                if quote.market == "BJ" or quote.code.startswith(("4", "8", "92")):
                    excluded["bse"] += 1
                    continue
                if not is_mainboard(quote.code):
                    excluded["other"] += 1
                    continue
                if quote.suspended:
                    excluded["suspended"] += 1
                    continue
                if quote.price is None or quote.pct_change is None:
                    excluded["invalid_quote"] += 1
                    continue
                if exclude_st and is_st(quote.name):
                    excluded["st"] += 1
                    continue
                if (quote.amount or 0) < min_amount:
                    excluded["illiquid"] += 1
                    continue
                if exclude_limit_up and at_price_limit(quote, "up"):
                    excluded["limit_untradable"] += 1
                    continue
                if exclude_limit_down and at_price_limit(quote, "down"):
                    excluded["limit_untradable"] += 1
                    continue
                if is_one_price_board(quote):
                    excluded["limit_untradable"] += 1
                    continue
                eligible.append(quote)

            pool, channel_counts = build_candidate_pool(eligible, target_size=pool_size)
            semaphore = asyncio.Semaphore(self.concurrency)
            kline_errors: Counter[str] = Counter()
            kline_sources: Counter[str] = Counter()
            provider_status = self.provider.health() if hasattr(self.provider, "health") else {}

            async def enrich(quote: Quote):
                try:
                    async with semaphore:
                        day, week = await asyncio.gather(
                            self.provider.get_kline(quote.code, "day", 260, "qfq", quote=quote),
                            self.provider.get_kline(quote.code, "week", 80, "qfq"),
                            return_exceptions=True,
                        )
                    day_klines = [] if isinstance(day, Exception) else day.klines
                    week_klines = [] if isinstance(week, Exception) else week.klines
                    if isinstance(day, Exception):
                        kline_errors[f"day:{type(day).__name__}:{str(day)[:160]}"] += 1
                    else:
                        kline_sources[day.source] += 1
                    if isinstance(week, Exception):
                        kline_errors[f"week:{type(week).__name__}:{str(week)[:160]}"] += 1
                    else:
                        kline_sources[week.source] += 1
                    return build_opportunity_candidate(
                        quote,
                        day_klines,
                        week_klines,
                        benchmarks.pct_for_market(quote.market),
                        provider_status,
                    )
                except Exception as exc:
                    kline_errors[f"candidate:{type(exc).__name__}:{str(exc)[:160]}"] += 1
                    return None

            enriched = await asyncio.gather(*(enrich(item) for item in pool))
            scored = sorted((item for item in enriched if item is not None), key=lambda item: item.opportunity_score, reverse=True)
            raw_top30 = scored[:top_n]
            action_top30 = raw_top30
            success = len(quotes)
            rate = success / total if total else 0.0
            data_ts = max((quote.data_timestamp for quote in quotes), default=now_shanghai())
            quality = self.quality.assess(data_ts, complete=bool(quotes))
            result = OpportunityScanResult(
                coverage=ScanCoverage(
                    total=total,
                    success=success,
                    filtered_mainboard=len(eligible),
                    failed=max(0, total - success),
                    coverage_rate=round(rate, 4),
                ),
                raw_top30=raw_top30,
                action_top30=action_top30,
                top100=scored[:100],
                candidate_pool_size=len(pool),
                channel_counts=channel_counts,
                scan_id=self.quality.scan_id(data_ts),
                score_formula=OPPORTUNITY_FORMULA,
                missing_data_sources=[
                    "fundamental_financials",
                    "valuation_industry_relative",
                    "announcements_news_policy_catalysts",
                    "main_or_big_order_flow",
                    "industry_classification_for_action_top30_concentration",
                ],
                duration_seconds=round(perf_counter() - scan_started, 3),
                **quality,
            )

            metrics_after = self.provider.metrics_snapshot() if hasattr(self.provider, "metrics_snapshot") else {}
            metrics = metric_delta(metrics_after, metrics_before) if metrics_after else {}
            health_after = self.provider.health() if hasattr(self.provider, "health") else {}

            def provider_delta(name: str, field: str) -> int:
                before = ((health_before.get("providers") or {}).get(name) or {}).get(field, 0)
                after = ((health_after.get("providers") or {}).get(name) or {}).get(field, 0)
                return int(after - before)

            self.last_summary = {
                "score_version": "v2",
                "stocks_total": total,
                "quotes_requested": total,
                "quotes_success": success,
                "quotes_failed": max(0, total - success),
                "filtered_mainboard": len(eligible),
                "candidate_pool_size": len(pool),
                "channel_counts": channel_counts,
                "excluded": dict(excluded),
                "kline_required": len(pool) * 2,
                "kline_cache_hit": metrics.get("cache_hit", 0),
                "kline_network_fetch": metrics.get("network_fetch", 0),
                "kline_success": metrics.get("success", 0),
                "kline_failed": metrics.get("failed", 0),
                "kline_stale_used": metrics.get("stale_used", 0),
                "eastmoney_success": provider_delta("eastmoney", "success_count"),
                "eastmoney_failure": provider_delta("eastmoney", "failure_count"),
                "eastmoney_empty": provider_delta("eastmoney", "empty_data_count"),
                "eastmoney_timeout": provider_delta("eastmoney", "timeout_count"),
                "tencent_success": provider_delta("tencent", "success_count"),
                "tencent_failure": provider_delta("tencent", "failure_count"),
                "kline_errors": dict(kline_errors.most_common(10)),
                "kline_sources": dict(kline_sources.most_common()),
                "scan_duration_seconds": result.duration_seconds,
            }
            logger.info("SCAN SUMMARY %s", json.dumps(self.last_summary, ensure_ascii=False, separators=(",", ":")))
            try:
                await save_scan_snapshot(
                    "v2",
                    result,
                    {
                        "top_n": top_n,
                        "pool_size": pool_size,
                        "min_amount": min_amount,
                        "exclude_st": exclude_st,
                        "exclude_limit_up": exclude_limit_up,
                        "exclude_limit_down": exclude_limit_down,
                    },
                )
            except Exception as exc:
                logger.warning("scan history save failed version=v2 error=%s", exc)
            return result

        return await self.cache.get_or_set(cache_key, 15, load)

    async def get_scan_coverage(self, scan_id: str | None = None) -> CoverageReport:
        if self.last_result is None or self.last_coverage is None:
            await self.scan_mainboard(top_n=1)
        assert self.last_result is not None and self.last_coverage is not None
        result = self.last_result
        stored = self._coverage_by_scan_id.get(scan_id or result.scan_id, self.last_coverage)
        current_freshness = self.quality.assess(
            stored.source_timestamp,
            timestamp_source=stored.timestamp_source,
            complete=stored.quotes_success > 0,
        )
        return stored.model_copy(
            update={
                **current_freshness,
                "data_age_seconds": current_freshness["age_seconds"],
            }
        )
