from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from app.models import (
    DataCoverage,
    DataQualityReport,
    FundamentalSnapshot,
    Kline,
    OpportunityCandidate,
    ScoreComponent,
    SupportResistance,
    TechnicalIndicators,
)
from app.services.fundamental_scoring import score_fundamental


OPPORTUNITY_FORMULA = (
    "opportunity_score = clamp(position_score(15) + fundamental_score(15) "
    "+ trend_score(20) + flow_score(15) + catalyst_score(10, missing in phase1) "
    "+ risk_reward_score(20) + liquidity_score(5) + risk_penalty(0..-20), 0, 100)"
)


@dataclass(frozen=True)
class Benchmarks:
    sh_pct: float = 0.0
    sz_pct: float = 0.0

    def pct_for_market(self, market: str) -> float:
        return self.sh_pct if market == "SH" else self.sz_pct


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def is_st_name(name: str) -> bool:
    normalized = name.upper().replace(" ", "")
    return normalized.startswith("ST") or normalized.startswith("*ST") or "退" in normalized


def at_price_limit(quote, direction: str) -> bool:
    if quote.price is None or quote.prev_close is None or quote.prev_close <= 0:
        return False
    ratio = 0.05 if is_st_name(quote.name) else 0.10
    target = round(quote.prev_close * (1 + ratio if direction == "up" else 1 - ratio) + 1e-8, 2)
    return quote.price >= target - 0.001 if direction == "up" else quote.price <= target + 0.001


def is_one_price_board(quote) -> bool:
    return quote.high is not None and quote.low is not None and quote.high == quote.low and (quote.volume or 0) > 0


def pct_change(current: float | None, base: float | None) -> float | None:
    if current is None or base in (None, 0):
        return None
    return (current / base - 1) * 100


def moving_average(values: list[float], days: int) -> float | None:
    if len(values) < days:
        return None
    return sum(values[-days:]) / days


def period_return(klines: list[Kline], days: int, price: float) -> float | None:
    if len(klines) <= days:
        return None
    base = klines[-days - 1].close
    return pct_change(price, base)


def window_low(klines: list[Kline], days: int) -> float | None:
    if len(klines) < days:
        return None
    return min(item.low for item in klines[-days:])


def window_high(klines: list[Kline], days: int) -> float | None:
    if len(klines) < days:
        return None
    return max(item.high for item in klines[-days:])


def location_between(price: float, low: float | None, high: float | None) -> float | None:
    if low is None or high is None or high <= low:
        return None
    return clamp((price - low) / (high - low), 0, 1)


def atr14(klines: list[Kline]) -> float | None:
    if len(klines) < 15:
        return None
    ranges: list[float] = []
    for index in range(1, len(klines)):
        current = klines[index]
        previous = klines[index - 1]
        ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    selected = ranges[-14:]
    return sum(selected) / len(selected) if len(selected) == 14 else None


def component(
    *,
    raw_value: dict[str, object] | None = None,
    normalized_value: float | dict[str, object] | None = None,
    score: float | None,
    max_score: float,
    reason: list[str] | None = None,
    data_source: list[str] | None = None,
    data_timestamp: datetime | None = None,
    coverage: bool,
) -> ScoreComponent:
    return ScoreComponent(
        raw_value=raw_value or {},
        normalized_value=normalized_value,
        score=None if score is None else round(score, 2),
        max_score=max_score,
        reason=reason or [],
        data_source=data_source or [],
        data_timestamp=data_timestamp,
        coverage=coverage,
    )


def build_candidate_pool(eligible: list, *, target_size: int = 420) -> tuple[list, dict[str, int]]:
    """Wide quote-only multi-channel pool. Final decisions happen after K-line scoring."""
    if not eligible:
        return [], {}
    target_size = min(max(target_size, 300), 500, len(eligible))
    channel_limit = max(80, math.ceil(target_size / 3))
    channels: list[tuple[str, list]] = []
    channels.append(("trend_improvement_proxy", sorted(eligible, key=lambda q: abs((q.pct_change or 0) - 1.0))[:channel_limit]))
    channels.append(("low_position_proxy", sorted(eligible, key=lambda q: (q.pct_change or 0, q.volume_ratio or 0))[:channel_limit]))
    channels.append(("flow_activity", sorted(eligible, key=lambda q: ((q.volume_ratio or 0), q.pct_change or -99), reverse=True)[:channel_limit]))
    channels.append(("relative_strength", sorted(eligible, key=lambda q: (q.pct_change or -99), reverse=True)[:channel_limit]))
    channels.append(("liquidity_floor", sorted(eligible, key=lambda q: (q.amount or 0), reverse=True)[:channel_limit]))

    pooled: OrderedDict[str, object] = OrderedDict()
    counts: dict[str, int] = {}
    for name, items in channels:
        before = len(pooled)
        for item in items:
            pooled.setdefault(item.code, item)
        counts[name] = len(pooled) - before
    if len(pooled) < target_size:
        for item in sorted(eligible, key=lambda q: (q.amount or 0), reverse=True):
            pooled.setdefault(item.code, item)
            if len(pooled) >= target_size:
                break
    return list(pooled.values())[:target_size], counts


def score_position(price: float, day: list[Kline], technical: TechnicalIndicators) -> ScoreComponent:
    highs = {days: window_high(day, days) for days in (20, 60, 120, 250)}
    lows = {days: window_low(day, days) for days in (20, 60, 120)}
    locations = {days: location_between(price, lows.get(days), highs.get(days)) for days in (20, 60, 120)}
    loc250 = location_between(price, window_low(day, 250), highs[250])
    returns = {days: period_return(day, days, price) for days in (20, 60, 120)}
    score = 0.0
    reasons: list[str] = []
    anchor = loc250 if loc250 is not None else locations.get(120) or locations.get(60)
    if anchor is not None:
        if 0.25 <= anchor <= 0.65:
            score += 5
            reasons.append("长期位置合理偏低")
        elif 0.12 <= anchor < 0.25:
            score += 3
            reasons.append("长期位置较低，需确认止跌")
        elif anchor > 0.8:
            reasons.append("接近长期高位")
        else:
            score += 1
    ret60 = returns[60]
    ret120 = returns[120]
    if ret60 is not None:
        if -12 <= ret60 <= 18:
            score += 3
            reasons.append("近60日未明显过热")
        elif ret60 > 35:
            reasons.append("近60日涨幅偏大")
    if ret120 is not None and ret120 < -25:
        score -= 2
        reasons.append("近120日跌幅较深，防止下跌惯性")
    if technical.ma20 and technical.ma60:
        distance20 = pct_change(price, technical.ma20)
        distance60 = pct_change(price, technical.ma60)
        if distance20 is not None and -4 <= distance20 <= 8:
            score += 3
            reasons.append("价格未明显远离MA20")
        if distance60 is not None and -8 <= distance60 <= 12:
            score += 2
            reasons.append("价格未明显远离MA60")
        if distance20 is not None and distance20 > 18:
            score -= 2
            reasons.append("短期偏离MA20较远")
    if len(day) >= 20:
        recent_range = highs[20] and lows[20] and (highs[20] - lows[20]) / max(price, 0.01)
        if recent_range is not None and recent_range < 0.22:
            score += 2
            reasons.append("近20日波动收敛")
    score = clamp(score, 0, 15)
    return component(
        raw_value={
            "location_250d": loc250,
            "location_120d": locations.get(120),
            "location_60d": locations.get(60),
            "distance_high_20d_pct": pct_change(price, highs[20]),
            "distance_high_60d_pct": pct_change(price, highs[60]),
            "distance_high_120d_pct": pct_change(price, highs[120]),
            "return_20d": returns[20],
            "return_60d": returns[60],
            "return_120d": returns[120],
        },
        normalized_value=anchor,
        score=score,
        max_score=15,
        reason=reasons,
        data_source=["day_kline"],
        data_timestamp=day[-1].timestamp if day else None,
        coverage=len(day) >= 120,
    )


def classify_week_trend(week: list[Kline]) -> tuple[str, float]:
    if len(week) < 30:
        return "UNKNOWN", 0.0
    closes = [item.close for item in week]
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)
    ma20_prev = sum(closes[-25:-5]) / 20 if len(closes) >= 25 else None
    recent_low = min(item.low for item in week[-8:])
    previous_low = min(item.low for item in week[-16:-8]) if len(week) >= 16 else recent_low
    recent_high = max(item.high for item in week[-8:])
    previous_high = max(item.high for item in week[-16:-8]) if len(week) >= 16 else recent_high
    slope = 0 if ma20 is None or ma20_prev in (None, 0) else (ma20 / ma20_prev - 1) * 100
    if ma10 and ma20 and ma10 > ma20 and slope > 0 and recent_low >= previous_low * 0.98:
        return "IMPROVING", 8.0
    if slope >= -2 and recent_low >= previous_low * 0.92:
        return "BASE_BUILDING", 5.0
    if slope < -2.5 and recent_high < previous_high * 1.03:
        return "DECLINING", 1.5
    return "MIXED", 4.0


def score_trend(price: float, day: list[Kline], week: list[Kline], technical: TechnicalIndicators) -> tuple[ScoreComponent, str, str]:
    week_label, week_score = classify_week_trend(week)
    closes = [item.close for item in day]
    ma5 = technical.ma5
    ma20 = technical.ma20
    ma60 = technical.ma60
    ma20_prev = sum(closes[-40:-20]) / 20 if len(closes) >= 40 else None
    ma60_prev = sum(closes[-120:-60]) / 60 if len(closes) >= 120 else None
    day_score = 0.0
    day_label = "UNKNOWN"
    reasons: list[str] = [f"周K={week_label}"]
    if ma20:
        ma20_slope = 0 if not ma20_prev else (ma20 / ma20_prev - 1) * 100
        if ma20_slope > 0:
            day_score += 3
            reasons.append("MA20开始向上")
        elif ma20_slope > -1:
            day_score += 2
            reasons.append("MA20由下降转平")
        if price > ma20:
            day_score += 3
            reasons.append("价格站上MA20")
    if ma60:
        ma60_slope = 0 if not ma60_prev else (ma60 / ma60_prev - 1) * 100
        if price > ma60:
            day_score += 2
            reasons.append("价格站上MA60")
        if ma60_slope > -1:
            day_score += 1
    if ma5 and ma20:
        recent_cross = len(day) < 8 or moving_average(closes[:-5], 5) is None or moving_average(closes[:-5], 20) is None
        if ma5 > ma20:
            day_score += 2 if recent_cross else 1.5
            reasons.append("MA5位于MA20上方")
    if len(day) >= 30:
        low_recent = min(item.low for item in day[-10:])
        low_prev = min(item.low for item in day[-30:-10])
        high_recent = max(item.high for item in day[-10:])
        high_prev = max(item.high for item in day[-30:-10])
        if low_recent >= low_prev * 0.98:
            day_score += 1.5
            reasons.append("近期低点未继续下移")
        if high_recent > high_prev * 1.01:
            day_score += 1.5
            reasons.append("短期高点抬高")
    if day_score >= 9:
        day_label = "TURNING_UP"
    elif day_score >= 6:
        day_label = "REPAIRING"
    elif day_score >= 3:
        day_label = "BASE_BUILDING"
    else:
        day_label = "WEAK"
    if week_label == "DECLINING":
        day_score = min(day_score, 5.0)
        reasons.append("周K仍下降，日K按反弹处理并限制趋势分")
    total = clamp(week_score + day_score, 0, 20)
    return (
        component(
            raw_value={"week_score": week_score, "day_score": day_score, "ma5": ma5, "ma20": ma20, "ma60": ma60},
            normalized_value={"week": week_label, "day": day_label},
            score=total,
            max_score=20,
            reason=reasons,
            data_source=["week_kline", "day_kline"],
            data_timestamp=max((series[-1].timestamp for series in (day, week) if series), default=None),
            coverage=len(day) >= 120 and len(week) >= 30,
        ),
        week_label,
        day_label,
    )


def score_flow(price: float, quote, day: list[Kline], benchmark_pct: float) -> ScoreComponent:
    score = 0.0
    reasons: list[str] = []
    volumes = [item.volume for item in day]
    avg20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
    current_volume = quote.volume or (volumes[-1] if volumes else None)
    volume_multiple = None if not avg20 or current_volume is None else current_volume / avg20
    if volume_multiple is not None:
        if 1.2 <= volume_multiple <= 3.0 and (quote.pct_change or 0) >= 0:
            score += 5
            reasons.append("上涨伴随温和放量")
        elif volume_multiple > 3.0:
            score += 2
            reasons.append("量能过高，可能拥挤")
        elif 0.7 <= volume_multiple < 1.2:
            score += 2
            reasons.append("量能未萎缩")
    if len(day) >= 6:
        last5 = day[-5:]
        up_volume = [item.volume for item in last5 if item.close >= item.open]
        down_volume = [item.volume for item in last5 if item.close < item.open]
        if up_volume and down_volume and sum(up_volume) / len(up_volume) > sum(down_volume) / len(down_volume):
            score += 3
            reasons.append("近5日上涨量能强于下跌量能")
        if last5[-1].close > max(item.high for item in day[-20:-1]) and volume_multiple and volume_multiple >= 1.2:
            score += 3
            reasons.append("放量突破近20日区间")
    excess = (quote.pct_change or 0) - benchmark_pct
    if excess > 0.5:
        score += 2
        reasons.append("跑赢指数")
    if quote.volume_ratio is not None and 0.9 <= quote.volume_ratio <= 2.5:
        score += 2
        reasons.append("量比活跃但不过热")
    return component(
        raw_value={"volume_multiple_20d": volume_multiple, "volume_ratio": quote.volume_ratio, "excess_pct": excess},
        normalized_value=volume_multiple,
        score=clamp(score, 0, 15),
        max_score=15,
        reason=reasons,
        data_source=["quote", "day_kline"],
        data_timestamp=max(day[-1].timestamp if day else quote.data_timestamp, quote.data_timestamp),
        coverage=bool(day) and avg20 is not None,
    )


def support_resistance(price: float, day: list[Kline], technical: TechnicalIndicators) -> SupportResistance:
    atr = atr14(day)
    lows = [value for value in (window_low(day, 20), window_low(day, 60), window_low(day, 120), technical.ma20, technical.ma60) if value is not None and value < price]
    highs = [value for value in (window_high(day, 20), window_high(day, 60), window_high(day, 120), window_high(day, 250)) if value is not None and value > price]
    support = max(lows) if lows else None
    resistance = min(highs) if highs else None
    if support is not None and atr is not None:
        stop = support - atr * 0.6
    elif support is not None:
        stop = support * 0.97
    else:
        stop = None
    target1 = resistance
    higher = [value for value in highs if target1 is None or value > target1 * 1.01]
    target2 = min(higher) if higher else None
    downside = None if stop is None or stop >= price else (price / stop - 1) * 100
    upside = None if target1 is None or target1 <= price else (target1 / price - 1) * 100
    ratio = None if downside in (None, 0) or upside is None else upside / downside
    return SupportResistance(
        support=None if support is None else round(support, 4),
        resistance=None if resistance is None else round(resistance, 4),
        stop_loss=None if stop is None else round(stop, 4),
        target_1=None if target1 is None else round(target1, 4),
        target_2=None if target2 is None else round(target2, 4),
        downside_pct=None if downside is None else round(downside, 4),
        upside_pct=None if upside is None else round(upside, 4),
        risk_reward_ratio=None if ratio is None else round(ratio, 4),
    )


def score_risk_reward(levels: SupportResistance, day: list[Kline]) -> ScoreComponent:
    rr = levels.risk_reward_ratio
    if rr is None:
        score = 0.0
        reasons = ["支撑或压力不足，无法形成可靠RR"]
    elif rr < 1:
        score = 0.0
        reasons = ["上涨空间小于下跌风险"]
    elif rr < 1.5:
        score = 4.0
        reasons = ["RR在1到1.5之间"]
    elif rr < 2:
        score = 8.0
        reasons = ["RR在1.5到2之间"]
    elif rr < 3:
        score = 14.0
        reasons = ["RR在2到3之间"]
    else:
        score = 20.0
        reasons = ["RR大于等于3"]
    return component(
        raw_value=levels.model_dump(),
        normalized_value=rr,
        score=score,
        max_score=20,
        reason=reasons,
        data_source=["day_kline"],
        data_timestamp=day[-1].timestamp if day else None,
        coverage=rr is not None,
    )


def score_liquidity(quote) -> ScoreComponent:
    amount = quote.amount or 0
    turnover = quote.turnover_rate
    score = 0.0
    reasons: list[str] = []
    if amount >= 50_000_000:
        score += 2
    if amount >= 100_000_000:
        score += 1
        reasons.append("成交额满足观察流动性")
    if turnover is not None and 0.5 <= turnover <= 15:
        score += 1.5
    if not quote.suspended and not is_one_price_board(quote):
        score += 0.5
    return component(
        raw_value={"amount": amount, "turnover_rate": turnover, "suspended": quote.suspended},
        normalized_value=amount,
        score=clamp(score, 0, 5),
        max_score=5,
        reason=reasons,
        data_source=["quote"],
        data_timestamp=quote.data_timestamp,
        coverage=quote.amount is not None,
    )


def score_missing_phase1(name: str, max_score: float, missing: list[str]) -> ScoreComponent:
    return component(
        raw_value={},
        normalized_value=None,
        score=None,
        max_score=max_score,
        reason=[f"{name}需要新增真实数据源，Phase1不伪造评分"],
        data_source=[],
        data_timestamp=None,
        coverage=False,
    )


def risk_penalty(quote, day: list[Kline], week_trend: str, levels: SupportResistance) -> ScoreComponent:
    penalty = 0.0
    reasons: list[str] = []
    ret20 = period_return(day, 20, quote.price)
    ret60 = period_return(day, 60, quote.price)
    if ret20 is not None and ret20 > 25:
        penalty -= 4
        reasons.append("近20日涨幅过大")
    if ret60 is not None and ret60 > 45:
        penalty -= 5
        reasons.append("近60日涨幅过大")
    if quote.volume_ratio is not None and quote.volume_ratio > 4:
        penalty -= 3
        reasons.append("量比极端，短线拥挤")
    if quote.turnover_rate is not None and quote.turnover_rate > 25:
        penalty -= 3
        reasons.append("换手率过高")
    if week_trend == "DECLINING":
        penalty -= 4
        reasons.append("周K明确下降")
    if levels.risk_reward_ratio is not None and levels.risk_reward_ratio < 1:
        penalty -= 4
        reasons.append("RR<1")
    if at_price_limit(quote, "up") or is_one_price_board(quote):
        penalty -= 20
        reasons.append("涨停或一字板，无法正常执行")
    return component(
        raw_value={"return_20d": ret20, "return_60d": ret60, "volume_ratio": quote.volume_ratio, "risk_reward_ratio": levels.risk_reward_ratio},
        normalized_value=penalty,
        score=clamp(penalty, -20, 0),
        max_score=20,
        reason=reasons,
        data_source=["quote", "day_kline"],
        data_timestamp=max(day[-1].timestamp if day else quote.data_timestamp, quote.data_timestamp),
        coverage=bool(day),
    )


def grade_for(score: float, breakdown: dict[str, ScoreComponent], week_trend: str, levels: SupportResistance, hard_reject: bool) -> str:
    if hard_reject:
        return "C"
    fundamentals_ok = breakdown["fundamental"].coverage and (breakdown["fundamental"].score or 0) >= 9
    catalysts_ok = breakdown["catalyst"].coverage
    if (
        score >= 80
        and fundamentals_ok
        and catalysts_ok
        and week_trend != "DECLINING"
        and levels.risk_reward_ratio is not None
        and levels.risk_reward_ratio >= 2
        and (breakdown["trend"].score or 0) >= 12
    ):
        return "A"
    if score >= 55 and week_trend != "DECLINING" and levels.risk_reward_ratio is not None and levels.risk_reward_ratio >= 1.3:
        return "B"
    return "C"


def build_opportunity_candidate(
    quote,
    day: list[Kline],
    week: list[Kline],
    benchmark_pct: float,
    provider_status: dict[str, object],
    fundamental_snapshot: FundamentalSnapshot | None = None,
) -> OpportunityCandidate:
    technical = TechnicalIndicators()
    if day:
        from app.indicators.technical import calculate_indicators

        technical = calculate_indicators(day, quote.price)
    position = score_position(quote.price, day, technical)
    trend, week_label, day_label = score_trend(quote.price, day, week, technical)
    flow = score_flow(quote.price, quote, day, benchmark_pct)
    levels = support_resistance(quote.price, day, technical)
    rr = score_risk_reward(levels, day)
    liquidity = score_liquidity(quote)
    fundamental, fundamental_risk = score_fundamental(fundamental_snapshot)
    catalyst = score_missing_phase1("催化", 10, [])
    technical_penalty = risk_penalty(quote, day, week_label, levels)
    combined_penalty_value = clamp((technical_penalty.score or 0) + (fundamental_risk.score or 0), -20, 0)
    penalty = component(
        raw_value={
            "technical": technical_penalty.raw_value,
            "fundamental": fundamental_risk.raw_value,
        },
        normalized_value=combined_penalty_value,
        score=combined_penalty_value,
        max_score=20,
        reason=[*technical_penalty.reason, *fundamental_risk.reason],
        data_source=list(dict.fromkeys([*technical_penalty.data_source, *fundamental_risk.data_source])),
        data_timestamp=max(
            (value for value in (technical_penalty.data_timestamp, fundamental_risk.data_timestamp) if value is not None),
            default=None,
        ),
        coverage=technical_penalty.coverage or fundamental_risk.coverage,
    )
    breakdown = {
        "position": position,
        "fundamental": fundamental,
        "trend": trend,
        "flow": flow,
        "catalyst": catalyst,
        "risk_reward": rr,
        "liquidity": liquidity,
        "fundamental_risk": fundamental_risk,
        "risk_penalty": penalty,
    }
    positive = sum(
        item.score or 0
        for key, item in breakdown.items()
        if key not in {"risk_penalty", "fundamental_risk"}
    )
    total = clamp(positive + (penalty.score or 0), 0, 100)
    missing = [key for key, item in breakdown.items() if not item.coverage]
    stale = []
    if quote.stale:
        stale.append("quote")
    hard_reject = at_price_limit(quote, "up") or is_one_price_board(quote)
    coverage = DataCoverage(
        quote=True,
        day_kline=len(day) >= 120,
        week_kline=len(week) >= 30,
        position=position.coverage,
        trend=trend.coverage,
        flow=flow.coverage,
        risk_reward=rr.coverage,
        liquidity=liquidity.coverage,
        fundamental=fundamental.coverage,
        catalyst=False,
    )
    quality = DataQualityReport(
        data_quality=quote.quality,
        coverage=coverage,
        stale_fields=stale,
        missing_fields=missing,
        conflict_fields=[] if fundamental_snapshot is None else [item.field for item in fundamental_snapshot.conflicts],
        provider_status=provider_status,
    )
    formula = (
        f"{OPPORTUNITY_FORMULA}; {round(total, 2)} = {position.score or 0} position + {fundamental.score or 0} fundamental + "
        f"{trend.score or 0} trend + {flow.score or 0} flow + 0 catalyst_missing + "
        f"{rr.score or 0} risk_reward + {liquidity.score or 0} liquidity + {penalty.score or 0} risk_penalty"
    )
    return OpportunityCandidate(
        stock_code=quote.code,
        stock_name=quote.name,
        price=quote.price,
        pct_change=quote.pct_change,
        amount=quote.amount,
        opportunity_score=round(total, 2),
        position_score=position.score or 0,
        fundamental_score=fundamental.score,
        trend_score=trend.score or 0,
        flow_score=flow.score or 0,
        catalyst_score=catalyst.score,
        risk_reward_score=rr.score or 0,
        liquidity_score=liquidity.score or 0,
        risk_penalty=penalty.score or 0,
        fundamental_risk_penalty=fundamental_risk.score or 0,
        grade=grade_for(total, breakdown, week_label, levels, hard_reject),
        support=levels.support,
        resistance=levels.resistance,
        stop_loss=levels.stop_loss,
        target_1=levels.target_1,
        target_2=levels.target_2,
        downside_pct=levels.downside_pct,
        upside_pct=levels.upside_pct,
        risk_reward_ratio=levels.risk_reward_ratio,
        week_trend=week_label,
        day_trend=day_label,
        data_coverage=coverage,
        data_quality=quality,
        entry_score=None,
        score_breakdown=breakdown,
        score_formula=formula,
        raw_inputs={
            "quote": {
                "price": quote.price,
                "pct_change": quote.pct_change,
                "amount": quote.amount,
                "turnover_rate": quote.turnover_rate,
                "volume_ratio": quote.volume_ratio,
            },
            "day_kline_count": len(day),
            "week_kline_count": len(week),
            "fundamental": None if fundamental_snapshot is None else fundamental_snapshot.model_dump(mode="json"),
        },
        reason=[
            *(position.reason[:2]),
            *(trend.reason[:3]),
            *(flow.reason[:2]),
            *(fundamental.reason[:2]),
            *(rr.reason[:1]),
            *(penalty.reason[:2]),
        ],
        hard_reject=hard_reject,
        snapshot_id=quote.snapshot_id,
    )
