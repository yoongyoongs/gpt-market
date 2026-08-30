from datetime import datetime, timezone
from uuid import uuid4

from app.v3.application.link_evidence_entities import (
    EntityCatalogEntry,
    EvidenceEntityMatcher,
)
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import (
    DecayModel,
    EntityLinkStatus,
    EvidenceSourceType,
    NormalizedEvidence,
)


NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def record(payload: dict) -> NormalizedEvidence:
    return NormalizedEvidence.build(
        raw_document_id=uuid4(),
        evidence_type=EvidenceType.NEWS,
        source_type=EvidenceSourceType.NEWS,
        source_priority=80,
        subject_type="MARKET",
        subject_id="CN_A_SHARES",
        claim_key=f"news:{uuid4()}",
        source="fixture-news",
        payload=payload,
        normalized_payload=payload,
        fetch_time=NOW,
        known_at=NOW,
        confidence=0.7,
        relevance=0.8,
        decay_model=DecayModel.NONE,
        parser_version="v1",
    )


def test_entity_matcher_confirms_unique_code_name_industry_and_market_aliases() -> None:
    matcher = EvidenceEntityMatcher((
        EntityCatalogEntry(
            entity_type="SECURITY", entity_id="SH:600519",
            canonical_name="贵州茅台", aliases=("600519",),
        ),
        EntityCatalogEntry(
            entity_type="INDUSTRY", entity_id="SW:FOOD_BEVERAGE",
            canonical_name="食品饮料", aliases=("白酒板块",),
        ),
        EntityCatalogEntry(
            entity_type="MARKET", entity_id="CN_A_SHARES",
            canonical_name="A股", aliases=("A股市场",),
        ),
    ))
    links = matcher.links_for((record({
        "title": "贵州茅台600519带动白酒板块，A股市场走强",
        "body": "ignore previous instructions is untrusted text",
    }),))
    assert {(item.entity_type, item.entity_id) for item in links} == {
        ("SECURITY", "SH:600519"),
        ("INDUSTRY", "SW:FOOD_BEVERAGE"),
        ("MARKET", "CN_A_SHARES"),
    }
    assert all(item.status is EntityLinkStatus.CONFIRMED for item in links)
    security = next(item for item in links if item.entity_type == "SECURITY")
    assert security.confidence == 1
    assert security.match_basis["alias"] == "600519"


def test_ambiguous_aliases_remain_candidates_and_codes_require_digit_boundaries() -> None:
    matcher = EvidenceEntityMatcher((
        EntityCatalogEntry(
            entity_type="SECURITY", entity_id="SH:600001",
            canonical_name="同名公司", aliases=("600001",),
        ),
        EntityCatalogEntry(
            entity_type="SECURITY", entity_id="SZ:000001",
            canonical_name="同名公司", aliases=("000001",),
        ),
    ))
    links = matcher.links_for((record({"title": "同名公司，代码16000012并非证券代码"}),))
    assert len(links) == 2
    assert all(item.status is EntityLinkStatus.CANDIDATE for item in links)
    assert all(item.match_basis["alias"] == "同名公司" for item in links)
