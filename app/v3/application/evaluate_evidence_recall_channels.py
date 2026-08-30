from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.recall import RecallChannel
from app.v3.providers.recall import (
    ChannelEvaluation,
    RecallCandidate,
    RecallChannelUnavailable,
)


def _number(value: object) -> float | None:
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


class FinancialImprovementRecallChannel:
    channel = RecallChannel.build(
        code="FUNDAMENTAL_IMPROVEMENT",
        version="evidence-recall-v1",
        configuration={
            "report_name": "RPT_F10_FINANCE_MAINFINADATA",
            "comparison": "same_period_previous_year",
            "minimum_growth": 0.1,
        },
        description="同报告期跨年营收或归母净利改善",
    )

    def evaluate(self, features, evidence) -> ChannelEvaluation:
        grouped = defaultdict(list)
        for item in evidence:
            payload = item.record.normalized_payload
            if payload.get("report_name") == self.channel.configuration["report_name"]:
                grouped[item.security_id].append(item)
        candidates = []
        evaluated = 0
        feature_by_id = {item.security_id: item for item in features}
        for security_id, items in grouped.items():
            by_period = {}
            for item in items:
                try:
                    period = date.fromisoformat(str(item.record.normalized_payload["report_period"]))
                except (KeyError, ValueError):
                    continue
                by_period[period] = item
            matched = None
            for period in sorted(by_period, reverse=True):
                try:
                    previous_period = period.replace(year=period.year - 1)
                except ValueError:
                    continue
                if previous_period in by_period:
                    matched = (by_period[period], by_period[previous_period])
                    break
            if matched is None:
                continue
            evaluated += 1
            current, previous = matched
            growth = {}
            for field in ("TOTALOPERATEREVE", "PARENTNETPROFIT"):
                current_value = _number(current.record.normalized_payload["values"].get(field))
                previous_value = _number(previous.record.normalized_payload["values"].get(field))
                if current_value is not None and previous_value not in {None, 0}:
                    growth[field] = current_value / abs(previous_value) - (1 if previous_value > 0 else -1)
            positive = {key: value for key, value in growth.items() if value >= 0.1}
            if not positive:
                continue
            feature = feature_by_id[security_id]
            candidates.append(RecallCandidate(
                security_id=security_id,
                strength=_clamp(max(positive.values()) / 0.5),
                reasons=tuple(f"same_period_growth:{key}={value:.4f}" for key, value in sorted(positive.items())),
                matched_features={
                    "current_evidence_id": str(current.record.evidence_id),
                    "previous_evidence_id": str(previous.record.evidence_id),
                    "growth": growth,
                },
                coverage=feature.coverage,
            ))
        if evaluated == 0:
            raise RecallChannelUnavailable(
                "FUNDAMENTAL_IMPROVEMENT has no same-period annual comparison"
            )
        candidates.sort(key=lambda item: (-item.strength, str(item.security_id)))
        return ChannelEvaluation(
            evaluated_count=evaluated,
            unavailable_count=len(features) - evaluated,
            candidates=tuple(candidates),
        )


class EarningsInflectionRecallChannel:
    REPORTS = {"RPT_PUBLIC_OP_NEWPREDICT", "RPT_FCI_PERFORMANCEE"}
    channel = RecallChannel.build(
        code="EARNINGS_INFLECTION",
        version="evidence-recall-v1",
        configuration={"minimum_yoy_percent": 20, "reports": sorted(REPORTS)},
        description="业绩预告或快报出现明确同比改善",
    )

    def evaluate(self, features, evidence) -> ChannelEvaluation:
        grouped = defaultdict(list)
        for item in evidence:
            if item.record.normalized_payload.get("report_name") in self.REPORTS:
                grouped[item.security_id].append(item)
        candidates = []
        evaluated = 0
        feature_by_id = {item.security_id: item for item in features}
        for security_id, items in grouped.items():
            latest = max(items, key=lambda item: (
                str(item.record.normalized_payload.get("report_period", "")),
                item.record.known_at,
            ))
            values = latest.record.normalized_payload.get("values", {})
            yoy_values = [
                _number(values.get(field))
                for field in ("ADD_AMP_LOWER", "ADD_AMP_UPPER", "YSTZ", "JLRTBZCL")
            ]
            available = [value for value in yoy_values if value is not None]
            if not available:
                continue
            evaluated += 1
            yoy = sum(available) / len(available)
            if yoy < 20:
                continue
            candidates.append(RecallCandidate(
                security_id=security_id,
                strength=_clamp(yoy / 100),
                reasons=(f"reported_yoy_percent={yoy:.2f}",),
                matched_features={
                    "evidence_id": str(latest.record.evidence_id),
                    "report_name": latest.record.normalized_payload["report_name"],
                    "yoy_percent": yoy,
                },
                coverage=feature_by_id[security_id].coverage,
            ))
        if evaluated == 0:
            raise RecallChannelUnavailable("EARNINGS_INFLECTION has no explicit yoy fields")
        candidates.sort(key=lambda item: (-item.strength, str(item.security_id)))
        return ChannelEvaluation(
            evaluated_count=evaluated,
            unavailable_count=len(features) - evaluated,
            candidates=tuple(candidates),
        )


class CatalystEventRecallChannel:
    KEYWORDS = ("回购", "增持", "中标", "重大合同", "业绩预增", "股权激励")
    channel = RecallChannel.build(
        code="CATALYST_EVENT",
        version="evidence-recall-v1",
        configuration={"official_title_keywords": KEYWORDS, "maximum_age_days": 30},
        description="近三十日官方公告出现显式催化事件",
    )

    def evaluate(self, features, evidence) -> ChannelEvaluation:
        feature_by_id = {item.security_id: item for item in features}
        grouped = defaultdict(list)
        for item in evidence:
            record = item.record
            published = record.publish_time or record.known_at
            feature = feature_by_id[item.security_id]
            if (
                record.evidence_type is EvidenceType.OFFICIAL_DISCLOSURE
                and published >= feature.as_of - timedelta(days=30)
                and published <= feature.as_of
            ):
                title = str(record.normalized_payload.get("title", ""))
                keywords = tuple(keyword for keyword in self.KEYWORDS if keyword in title)
                grouped[item.security_id].append((item, title, keywords, published))
        evaluated = len(grouped)
        if evaluated == 0:
            raise RecallChannelUnavailable("CATALYST_EVENT has no official disclosures")
        candidates = []
        for security_id, items in grouped.items():
            matched = [item for item in items if item[2]]
            if not matched:
                continue
            matched.sort(key=lambda item: (item[3], str(item[0].record.evidence_id)), reverse=True)
            evidence_item, title, keywords, _ = matched[0]
            candidates.append(RecallCandidate(
                security_id=security_id,
                strength=_clamp(0.5 + 0.1 * len(keywords) + 0.2 * evidence_item.effective_relevance),
                reasons=tuple(f"official_event_keyword={keyword}" for keyword in keywords),
                matched_features={
                    "evidence_id": str(evidence_item.record.evidence_id),
                    "title": title,
                    "keywords": keywords,
                },
                coverage=feature_by_id[security_id].coverage,
            ))
        candidates.sort(key=lambda item: (-item.strength, str(item.security_id)))
        return ChannelEvaluation(
            evaluated_count=evaluated,
            unavailable_count=len(features) - evaluated,
            candidates=tuple(candidates),
        )


def evidence_recall_channels():
    return (
        FinancialImprovementRecallChannel(),
        EarningsInflectionRecallChannel(),
        CatalystEventRecallChannel(),
    )
