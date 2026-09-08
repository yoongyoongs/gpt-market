"""L2 证据类召回专家（设计 §11-§12：FQ/CAT）。

输入为该股 NormalizedEvidence 视图（SecurityEvidenceView），
证据判据复用现有 evidence recall 通道的 payload 约定：
- 财务主表 RPT_F10_FINANCE_MAINFINADATA（同报告期跨年对比）；
- 业绩预告/快报 RPT_PUBLIC_OP_NEWPREDICT / RPT_FCI_PERFORMANCEE；
- 官方公告 OFFICIAL_DISCLOSURE + 催化关键词（30 日内）。

设计 §11.4 的 8 个基本面字段当前证据库无结构化数据 →
可评维度只有营收/利润/业绩预告三类，其余 missing 降 confidence。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.v3.candidate_engine.experts.base import BaseExpert
from app.v3.candidate_engine.soft import soft_high_better
from app.v3.domain.candidate_engine import ExpertInput, ExpertScore
from app.v3.domain.evidence import NormalizedEvidence, SecurityEvidenceView

__all__ = ["FundamentalRepairExpert", "CatalystExpert"]

FINANCE_REPORT = "RPT_F10_FINANCE_MAINFINADATA"
PREDICT_REPORTS = ("RPT_PUBLIC_OP_NEWPREDICT", "RPT_FCI_PERFORMANCEE")
REVENUE_FIELD = "TOTALOPERATEREVE"
PROFIT_FIELD = "PARENTNETPROFIT"
PREDICT_FIELDS = ("ADD_AMP_LOWER", "ADD_AMP_UPPER", "YSTZ", "JLRTBZCL")
CATALYST_KEYWORDS = ("回购", "增持", "中标", "重大合同", "业绩预增", "股权激励")
CATALYST_MAX_AGE_DAYS = 30
CATALYST_TAU_DAYS = 10.0
# 催化后 5 日涨幅达到该幅度视为已定价（unpriced → 0）
UNPRICED_RETURN_CAP = 0.20

# 设计 §12.2 Credibility：source_priority 越小越权威
CREDIBILITY_BY_PRIORITY = {1: 1.0, 2: 0.85, 3: 0.6}


def _number(value: object) -> float | None:
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _same_period_growth(
    items: list[SecurityEvidenceView],
) -> tuple[float | None, float | None]:
    """同报告期跨年对比（复用 FUNDAMENTAL_IMPROVEMENT 通道语义）。

    返回 (营收同比, 利润同比)；无法配对返回 (None, None)。
    """
    by_period: dict[datetime, NormalizedEvidence] = {}
    for item in items:
        payload = item.record.normalized_payload
        if payload.get("report_name") != FINANCE_REPORT:
            continue
        try:
            period = datetime.fromisoformat(str(payload["report_period"]))
        except (KeyError, ValueError):
            continue
        existing = by_period.get(period)
        if existing is None or item.record.known_at > existing.known_at:
            by_period[period] = item.record
    for period in sorted(by_period, reverse=True):
        previous = None
        try:
            previous = by_period.get(period.replace(year=period.year - 1))
        except ValueError:
            continue
        if previous is None:
            continue
        growth: list[float | None] = []
        for field in (REVENUE_FIELD, PROFIT_FIELD):
            current_value = _number(by_period[period].normalized_payload["values"].get(field))
            previous_value = _number(previous.normalized_payload["values"].get(field))
            if current_value is None or previous_value in {None, 0}:
                growth.append(None)
                continue
            assert previous_value is not None
            growth.append(
                current_value / abs(previous_value) - (1 if previous_value > 0 else -1)
            )
        return growth[0], growth[1]
    return None, None


def _predict_yoy(items: list[SecurityEvidenceView]) -> float | None:
    """业绩预告/快报同比（取 report_period 最新一条的均值口径）。"""
    latest: SecurityEvidenceView | None = None
    latest_key: tuple[str, datetime] | None = None
    for item in items:
        payload = item.record.normalized_payload
        if payload.get("report_name") not in PREDICT_REPORTS:
            continue
        key = (str(payload.get("report_period", "")), item.record.known_at)
        if latest_key is None or key > latest_key:
            latest, latest_key = item, key
    if latest is None:
        return None
    values = latest.record.normalized_payload.get("values", {})
    available = [v for v in (_number(values.get(field)) for field in PREDICT_FIELDS) if v is not None]
    if not available:
        return None
    return sum(available) / len(available)


class FundamentalRepairExpert(BaseExpert):
    """Expert 7：FQ 基本面修复（设计 §11.4，Top150）。

    无基本面改善证据的股票不由本专家召回（返回 None）；
    有证据但字段缺失的维度按 missing 降 confidence（设计 §11.3）。
    """

    def name(self) -> str:
        return "FQ"

    def top_n(self) -> int:
        return 150

    def required_features(self) -> tuple[str, ...]:
        return ()

    def evaluate(self, stock: ExpertInput) -> ExpertScore | None:
        if not stock.evidence:
            return None
        revenue_growth, profit_growth = _same_period_growth(list(stock.evidence))
        predict_yoy = _predict_yoy(list(stock.evidence))
        if revenue_growth is None and profit_growth is None and predict_yoy is None:
            return None

        def improvement(value: float | None, threshold: float) -> float | None:
            if value is None:
                return None
            return soft_high_better(value, threshold, threshold * 5) / 100.0

        relevance = [
            item.effective_relevance
            for item in stock.evidence
            if item.record.normalized_payload.get("report_name")
            in (FINANCE_REPORT, *PREDICT_REPORTS)
        ]
        evidence_confidence = (
            sum(relevance) / len(relevance) if relevance else 0.0
        )
        # 可评权重 45/100（营收 15 + 利润 20 + 业绩预告 10）
        score = self.combine(
            [
                ("revenue_trend", 15.0, improvement(revenue_growth, 0.10)),
                ("profit_trend", 20.0, improvement(profit_growth, 0.10)),
                ("earnings_guidance", 10.0, improvement(
                    None if predict_yoy is None else predict_yoy / 100.0, 0.20
                )),
                ("operating_cashflow", 15.0, None),
                ("margin_delta", 10.0, None),
                ("roe_trend", 10.0, None),
                ("debt_risk", 10.0, None),
                ("valuation_percentile", 10.0, None),
            ],
            extra_confidence=evidence_confidence,
        )
        if score.value <= 0:
            return None
        return score


class CatalystExpert(BaseExpert):
    """Expert 8：CAT 催化（设计 §12.2，Top150）。

    CatalystScore = Credibility × Strength × Freshness × UnpricedFactor。
    post_event_return 无事件日价格 → 用近 5 日涨幅代理已定价程度。
    """

    def name(self) -> str:
        return "CAT"

    def top_n(self) -> int:
        return 150

    def required_features(self) -> tuple[str, ...]:
        return ()

    def evaluate(self, stock: ExpertInput) -> ExpertScore | None:
        view = stock.feature
        as_of = view.as_of
        if as_of is None:
            return None
        best: tuple[float, float, float] | None = None  # (score, credibility, relevance)
        for item in stock.evidence:
            record = item.record
            if record.evidence_type.value != "OFFICIAL_DISCLOSURE":
                continue
            published = record.publish_time or record.known_at
            if published > as_of or published < as_of - timedelta(days=CATALYST_MAX_AGE_DAYS):
                continue
            title = str(record.normalized_payload.get("title", ""))
            keywords = tuple(k for k in CATALYST_KEYWORDS if k in title)
            if not keywords:
                continue
            credibility = CREDIBILITY_BY_PRIORITY.get(record.source_priority, 0.4)
            strength = min(1.0, 0.5 + 0.1 * len(keywords) + 0.2 * item.effective_relevance)
            days = max(0.0, (as_of - published).total_seconds() / 86400)
            freshness = math.exp(-days / CATALYST_TAU_DAYS)
            candidate = (credibility * strength * freshness, credibility, item.effective_relevance)
            if best is None or candidate[0] > best[0]:
                best = candidate
        if best is None:
            return None
        raw_score, credibility, relevance = best
        return_5d = view.number("return_5d")
        unpriced = (
            1.0 if return_5d is None
            else 1.0 - max(0.0, min(return_5d, UNPRICED_RETURN_CAP)) / UNPRICED_RETURN_CAP
        )
        score = self.combine(
            [("catalyst", 100.0, raw_score * unpriced)],
            extra_confidence=max(relevance, 0.3),
        )
        if score.value <= 0:
            return None
        score.features_used.update({
            "credibility": credibility,
            "unpriced": unpriced,
        })
        return score
