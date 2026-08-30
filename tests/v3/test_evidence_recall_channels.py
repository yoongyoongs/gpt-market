from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.v3.application.evaluate_evidence_recall_channels import evidence_recall_channels
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import (
    DecayModel,
    EvidenceSourceType,
    NormalizedEvidence,
    SecurityEvidenceView,
)
from tests.v3.test_recall_channels import feature


NOW = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)


def evidence_view(security_id, *, report_name=None, period=None, values=None, title=None):
    official = title is not None
    normalized = (
        {"title": title, "publish_date": "2026-08-30"}
        if official
        else {
            "report_name": report_name,
            "report_period": period,
            "values": values or {},
        }
    )
    record = NormalizedEvidence.build(
        raw_document_id=uuid4(),
        evidence_type=(EvidenceType.OFFICIAL_DISCLOSURE if official else EvidenceType.VENDOR_DATA),
        source_type=(EvidenceSourceType.OFFICIAL if official else EvidenceSourceType.VENDOR),
        source_priority=1 if official else 50,
        subject_type="SECURITY", subject_id="SH:600519",
        claim_key=f"fixture:{uuid4()}", source="fixture",
        payload=normalized, normalized_payload=normalized,
        publish_time=NOW - timedelta(days=1),
        fetch_time=NOW - timedelta(hours=1), known_at=NOW - timedelta(hours=1),
        confidence=1 if official else 0.8, relevance=0.9,
        decay_model=DecayModel.NONE, parser_version="v1",
    )
    return SecurityEvidenceView(
        security_id=security_id, record=record, effective_relevance=0.9,
    )


def test_three_evidence_channels_use_explicit_report_and_official_event_fields() -> None:
    row = feature(as_of=NOW)
    evidence = (
        evidence_view(
            row.security_id, report_name="RPT_F10_FINANCE_MAINFINADATA",
            period="2025-06-30", values={"TOTALOPERATEREVE": 100, "PARENTNETPROFIT": 10},
        ),
        evidence_view(
            row.security_id, report_name="RPT_F10_FINANCE_MAINFINADATA",
            period="2026-06-30", values={"TOTALOPERATEREVE": 130, "PARENTNETPROFIT": 15},
        ),
        evidence_view(
            row.security_id, report_name="RPT_PUBLIC_OP_NEWPREDICT",
            period="2026-06-30", values={"ADD_AMP_LOWER": 30, "ADD_AMP_UPPER": 50},
        ),
        evidence_view(row.security_id, title="关于股份回购暨增持计划的公告"),
    )
    results = {
        channel.channel.code: channel.evaluate((row,), evidence)
        for channel in evidence_recall_channels()
    }
    assert set(results) == {
        "FUNDAMENTAL_IMPROVEMENT", "EARNINGS_INFLECTION", "CATALYST_EVENT",
    }
    assert all(len(result.candidates) == 1 for result in results.values())
    assert results["FUNDAMENTAL_IMPROVEMENT"].candidates[0].matched_features[
        "current_evidence_id"
    ]
    assert results["EARNINGS_INFLECTION"].candidates[0].matched_features[
        "yoy_percent"
    ] == 40
    assert results["CATALYST_EVENT"].candidates[0].reasons == (
        "official_event_keyword=回购", "official_event_keyword=增持",
    )


def test_official_disclosure_without_whitelisted_event_is_evaluated_but_not_hit() -> None:
    row = feature(as_of=NOW)
    catalyst = next(
        channel for channel in evidence_recall_channels()
        if channel.channel.code == "CATALYST_EVENT"
    )
    result = catalyst.evaluate(
        (row,), (evidence_view(row.security_id, title="董事会会议决议公告"),)
    )
    assert result.evaluated_count == 1
    assert result.candidates == ()
