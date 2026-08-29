from __future__ import annotations

from app.models import FundamentalSnapshot, ScoreComponent


def _value(snapshot: FundamentalSnapshot, name: str) -> float | None:
    field = snapshot.fields.get(name)
    if field is None or not field.coverage or not isinstance(field.value, (int, float)):
        return None
    return float(field.value)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def score_fundamental(snapshot: FundamentalSnapshot | None) -> tuple[ScoreComponent, ScoreComponent]:
    if snapshot is None or snapshot.coverage < 0.45:
        error = None if snapshot is None else snapshot.error
        missing = ScoreComponent(
            raw_value={} if snapshot is None else snapshot.model_dump(mode="json"),
            normalized_value=None,
            score=None,
            max_score=15,
            reason=["基本面核心字段覆盖不足，未评分"],
            data_source=[] if snapshot is None else snapshot.upstream_sources,
            data_timestamp=None if snapshot is None else snapshot.fetch_time,
            coverage=False,
        )
        risk = ScoreComponent(
            raw_value={"error": error}, normalized_value=0, score=0, max_score=20,
            reason=["基本面数据不足，不将缺失按风险零分或负分处理"], data_source=[],
            data_timestamp=None if snapshot is None else snapshot.fetch_time, coverage=False,
        )
        return missing, risk

    reasons: list[str] = []
    dimensions: list[tuple[float, float]] = []
    revenue_yoy = _value(snapshot, "revenue_yoy")
    revenue_qoq = _value(snapshot, "revenue_qoq")
    profit_yoy = _value(snapshot, "net_profit_yoy")
    profit_qoq = _value(snapshot, "net_profit_qoq")
    deducted_yoy = _value(snapshot, "deducted_net_profit")
    latest_profit = _value(snapshot, "net_profit")
    latest_deducted = _value(snapshot, "deducted_net_profit")
    revenue = _value(snapshot, "revenue")
    forecast_value = (
        snapshot.performance_forecast.value
        if snapshot.performance_forecast and snapshot.performance_forecast.coverage and isinstance(snapshot.performance_forecast.value, dict)
        else None
    )
    express_value = (
        snapshot.performance_express.value
        if snapshot.performance_express and snapshot.performance_express.coverage and isinstance(snapshot.performance_express.value, dict)
        else None
    )

    improvement_inputs = [revenue_yoy, revenue_qoq, profit_yoy, profit_qoq]
    if any(value is not None for value in improvement_inputs):
        score = 0.0
        if revenue_yoy is not None and revenue_yoy > 10:
            score += 1.0
            reasons.append("营收同比增长")
        if revenue_qoq is not None and revenue_qoq > 0:
            score += 0.75
            reasons.append("营收环比改善")
        previous_profit = snapshot.quarterly_trend[1].net_profit if len(snapshot.quarterly_trend) > 1 else None
        previous_revenue = snapshot.quarterly_trend[1].revenue if len(snapshot.quarterly_trend) > 1 else revenue
        credible_growth = previous_profit is not None and previous_profit > 0 and (_ratio(previous_profit, previous_revenue) or 0) > 0.005
        if profit_yoy is not None and profit_yoy > 10 and credible_growth:
            score += 1.25
            reasons.append("净利润同比增长且非低基数")
        elif profit_yoy is not None and profit_yoy > 50:
            reasons.append("净利润高增长但基数质量不足，未足额加分")
        if profit_qoq is not None and profit_qoq > 0 and latest_profit is not None and latest_profit > 0:
            score += 0.75
            reasons.append("净利润环比改善")
        quarters = [row for row in snapshot.quarterly_trend[:4] if row.net_profit_yoy is not None]
        if len(quarters) >= 3 and quarters[0].net_profit_yoy > quarters[-1].net_profit_yoy:
            score += 1.25
            reasons.append("近季度利润趋势改善")
        if forecast_value:
            lower = forecast_value.get("yoy_lower")
            upper = forecast_value.get("yoy_upper")
            if isinstance(lower, (int, float)) and isinstance(upper, (int, float)) and lower > 0 and upper > 0:
                score += 0.5
                reasons.append("最新业绩预告区间为正增长")
        if express_value:
            express_yoy = express_value.get("net_profit_yoy")
            express_period = snapshot.performance_express.report_period if snapshot.performance_express else None
            if express_period and express_period > (snapshot.report_period or "") and isinstance(express_yoy, (int, float)) and express_yoy > 10:
                score += 0.5
                reasons.append("更新报告期的业绩快报利润改善")
        dimensions.append((min(score, 5.0), 5.0))

    ocf = _value(snapshot, "operating_cash_flow")
    gross_margin = _value(snapshot, "gross_margin")
    if any(value is not None for value in (latest_profit, latest_deducted, ocf, gross_margin)):
        score = 0.0
        deduct_ratio = _ratio(latest_deducted, latest_profit)
        cash_ratio = _ratio(ocf, latest_profit)
        if latest_profit is not None and latest_profit > 0:
            score += 0.5
        if deduct_ratio is not None and deduct_ratio >= 0.8:
            score += 1.0
            reasons.append("扣非利润与归母利润匹配")
        if cash_ratio is not None and cash_ratio >= 0.8:
            score += 1.0
            reasons.append("经营现金流覆盖利润")
        if gross_margin is not None and gross_margin >= 20:
            score += 0.5
        dimensions.append((min(score, 3.0), 3.0))

    roe = _value(snapshot, "roe")
    if roe is not None:
        score = 2.0 if roe >= 15 else 1.5 if roe >= 10 else 0.75 if roe >= 6 else 0.0
        dimensions.append((score, 2.0))
        if score >= 1.5:
            reasons.append("ROE较好")

    debt = _value(snapshot, "debt_ratio")
    if debt is not None:
        score = 2.0 if debt < 40 else 1.25 if debt < 60 else 0.5 if debt < 75 else 0.0
        dimensions.append((score, 2.0))
        if score >= 1.25:
            reasons.append("负债率处于合理区间")

    pe = _value(snapshot, "pe")
    pb = _value(snapshot, "pb")
    industry_pe = _value(snapshot, "industry_pe_median")
    industry_pb = _value(snapshot, "industry_pb_median")
    if any(value is not None for value in (pe, pb)):
        score = 0.0
        available = 0.0
        if pe is not None and pe > 0:
            available += 1.5
            score += 1.5 if industry_pe and pe <= industry_pe else 0.75 if pe <= 35 else 0.25
        if pb is not None and pb > 0:
            available += 1.5
            score += 1.5 if industry_pb and pb <= industry_pb else 0.75 if pb <= 4 else 0.25
        if available:
            dimensions.append((score, available))
            if score >= available * 0.75:
                reasons.append("估值不高于候选池行业中位数")

    available_max = sum(maximum for _, maximum in dimensions)
    raw_score = sum(value for value, _ in dimensions)
    normalized = raw_score / available_max * 15 if available_max else None
    if normalized is not None and snapshot.coverage < 0.75:
        normalized = min(normalized, 12.0)
    score_value = None if normalized is None else round(max(0.0, min(15.0, normalized)), 2)
    component = ScoreComponent(
        raw_value={
            "fields": {name: field.model_dump(mode="json") for name, field in snapshot.fields.items()},
            "quarterly_trend": [row.model_dump(mode="json") for row in snapshot.quarterly_trend],
            "report_period": snapshot.report_period,
            "coverage_rate": snapshot.coverage,
            "conflicts": [item.model_dump(mode="json") for item in snapshot.conflicts],
        },
        normalized_value={"available_points": available_max, "raw_points": raw_score, "coverage_rate": snapshot.coverage},
        score=score_value,
        max_score=15,
        reason=reasons or ["基本面表现中性"],
        data_source=snapshot.upstream_sources,
        data_timestamp=snapshot.fetch_time,
        coverage=score_value is not None,
    )

    penalty = 0.0
    risk_reasons: list[str] = []
    recent = snapshot.quarterly_trend[:4]
    profits = [row.net_profit for row in recent if row.net_profit is not None]
    if len(profits) >= 3 and all(value <= 0 for value in profits[:3]):
        penalty -= 4
        risk_reasons.append("最近三期持续亏损")
    if (profit_yoy is not None and profit_yoy < -30) or any(
        row.deducted_net_profit_yoy is not None and row.deducted_net_profit_yoy < -30 for row in recent[:2]
    ):
        penalty -= 3
        risk_reasons.append("利润或扣非利润明显恶化")
    cash_anomalies = sum(
        row.net_profit is not None and row.net_profit > 0 and row.operating_cash_flow is not None and row.operating_cash_flow < 0
        for row in recent[:3]
    )
    if cash_anomalies >= 2:
        penalty -= 3
        risk_reasons.append("盈利期间经营现金流连续为负")
    if debt is not None and debt >= 80:
        penalty -= 3
        risk_reasons.append("负债率异常偏高")
    deduct_ratio = _ratio(latest_deducted, latest_profit)
    if deduct_ratio is not None and latest_profit and latest_profit > 0 and deduct_ratio < 0.5:
        penalty -= 3
        risk_reasons.append("扣非利润显著低于归母利润，关注一次性收益")
    if forecast_value:
        forecast_type = str(forecast_value.get("type") or "")
        forecast_upper = forecast_value.get("yoy_upper")
        if any(word in forecast_type for word in ("首亏", "续亏", "预减")) or (
            isinstance(forecast_upper, (int, float)) and forecast_upper < -30
        ):
            penalty -= 2
            risk_reasons.append("最新业绩预告显示亏损或明显下滑")
    if express_value and isinstance(express_value.get("net_profit_yoy"), (int, float)) and express_value["net_profit_yoy"] < -30:
        penalty -= 2
        risk_reasons.append("业绩快报净利润明显下滑")
    audit = snapshot.audit_opinion
    if audit and audit.coverage and isinstance(audit.value, str) and any(
        word in audit.value for word in ("保留意见", "否定意见", "无法表示意见", "非标准")
    ):
        penalty -= 8
        risk_reasons.append("存在非标准审计意见")
    risk = ScoreComponent(
        raw_value={"cash_anomaly_periods": cash_anomalies, "debt_ratio": debt, "audit_opinion": None if audit is None else audit.model_dump(mode="json")},
        normalized_value=penalty,
        score=max(-12.0, penalty),
        max_score=20,
        reason=risk_reasons,
        data_source=snapshot.upstream_sources,
        data_timestamp=snapshot.fetch_time,
        coverage=True,
    )
    return component, risk
