from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from app.utils.time import SHANGHAI
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import (
    EntityLinkStatus,
    EvidenceSourceType,
    RawDocument,
)
from app.v3.infrastructure.providers.evidence import (
    CNINFO_QUERY_URL,
    EASTMONEY_DATACENTER_URL,
    EASTMONEY_NEWS_URL,
    GOV_POLICY_URL,
    CninfoAnnouncementParser,
    CninfoAnnouncementProvider,
    EastmoneyNewsParser,
    EastmoneyNewsProvider,
    EastmoneyReportParser,
    EastmoneyReportProvider,
    EvidenceCapability,
    EvidenceProviderRegistry,
    GovernmentPolicyParser,
    GovernmentPolicyProvider,
)


START = datetime(2026, 8, 29, tzinfo=SHANGHAI)
END = datetime(2026, 8, 31, tzinfo=SHANGHAI)


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def raw_from(provider, fetched) -> RawDocument:
    return RawDocument.build(
        evidence_source_id=provider.source.evidence_source_id,
        fetched=fetched,
        normalized_reference=fetched.raw_reference,
    )


@pytest.mark.asyncio
async def test_cninfo_provider_and_parser_preserve_official_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CNINFO_QUERY_URL
        assert b"seDate=2026-08-29~2026-08-31" in request.content
        return httpx.Response(
            200,
            json={
                "totalRecordNum": 1,
                "announcements": [
                    {
                        "secCode": "600519",
                        "secName": "贵州茅台",
                        "announcementId": "1225534028",
                        "announcementTitle": "2026年半年度报告",
                        "announcementTime": 1787932800000,
                        "adjunctUrl": "finalpage/2026-08-29/1225534028.PDF",
                        "adjunctType": "PDF",
                        "announcementType": "01010503||010123",
                    }
                ],
            },
        )

    provider = CninfoAnnouncementProvider(
        page_size=30, client=client_for(handler), retries=0
    )
    batch = await provider.fetch(window_start=START, window_end=END, cursor=None)
    assert batch.exhausted is True
    assert batch.upstream_count == 1
    assert batch.documents[0].document_key == "announcement:1225534028"
    assert batch.documents[0].raw_reference.endswith("1225534028.PDF")

    parsed = CninfoAnnouncementParser().parse(
        raw_from(provider, batch.documents[0]), provider.source
    )
    assert parsed.records[0].evidence_type is EvidenceType.OFFICIAL_DISCLOSURE
    assert parsed.records[0].source_type is EvidenceSourceType.OFFICIAL
    assert parsed.records[0].subject_id == "SH:600519"
    assert parsed.links[0].status is EntityLinkStatus.CONFIRMED
    await provider.close()


@pytest.mark.asyncio
async def test_eastmoney_report_provider_pages_codes_and_normalizes_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.copy_with(query=None) == httpx.URL(EASTMONEY_DATACENTER_URL)
        assert request.url.params["reportName"] == "RPT_F10_FINANCE_MAINFINADATA"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "data": [
                        {
                            "SECURITY_CODE": "600519",
                            "REPORT_DATE": "2026-06-30 00:00:00",
                            "NOTICE_DATE": "2026-08-15 00:00:00",
                            "TOTALOPERATEREVE": 92278072083.21,
                            "PARENTNETPROFIT": 44516880421.86,
                            "KCFJCXSYJLR": 44464207646.01,
                            "NETCASH_OPERATE_PK": 70690750119.06,
                            "ROEJQ": 16.75,
                            "XSMLL": 89.55,
                            "ZCFZL": 15.19,
                        }
                    ]
                },
            },
        )

    provider = EastmoneyReportProvider(
        report_name="RPT_F10_FINANCE_MAINFINADATA",
        codes=("600519", "000001"),
        chunk_size=1,
        client=client_for(handler),
        retries=0,
    )
    batch = await provider.fetch(window_start=None, window_end=None, cursor=None)
    assert batch.exhausted is False
    assert batch.next_cursor == {"code_offset": 1}
    parsed = EastmoneyReportParser().parse(
        raw_from(provider, batch.documents[0]), provider.source
    )
    record = parsed.records[0]
    assert record.evidence_type is EvidenceType.VENDOR_DATA
    assert record.subject_id == "SH:600519"
    assert record.normalized_payload["values"]["ROEJQ"] == 16.75
    assert parsed.links[0].status is EntityLinkStatus.CONFIRMED
    await provider.close()


@pytest.mark.asyncio
async def test_government_policy_provider_filters_window_and_parses_market_fact() -> None:
    rows = [
        {
            "TITLE": "国务院关于测试政策的通知",
            "SUB_TITLE": "",
            "URL": "https://www.gov.cn/zhengce/content/202608/content_1.htm",
            "DOCRELPUBTIME": "2026-08-30",
        },
        {
            "TITLE": "窗口外政策",
            "SUB_TITLE": "",
            "URL": "https://www.gov.cn/zhengce/content/202607/content_2.htm",
            "DOCRELPUBTIME": "2026-07-01",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GOV_POLICY_URL
        return httpx.Response(200, json=rows)

    provider = GovernmentPolicyProvider(client=client_for(handler), retries=0)
    batch = await provider.fetch(window_start=START, window_end=END, cursor=None)
    assert batch.upstream_count == 1
    assert len(batch.documents) == 1
    parsed = GovernmentPolicyParser().parse(
        raw_from(provider, batch.documents[0]), provider.source
    )
    assert parsed.records[0].evidence_type is EvidenceType.FACT
    assert parsed.records[0].subject_id == "CN_A_SHARES"
    assert parsed.records[0].source_type is EvidenceSourceType.OFFICIAL
    await provider.close()


@pytest.mark.asyncio
async def test_eastmoney_news_provider_uses_article_date_and_candidate_scope() -> None:
    html = """
    <div><p class="title"><a href="https://finance.eastmoney.com/a/202608303859415261.html">
      市场新闻标题
    </a></p><p class="time">8月30日 16:31</p></div>
    <div><p class="title"><a href="https://example.com/not-news.html">忽略</a></p>
      <p class="time">8月30日 12:00</p></div>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == EASTMONEY_NEWS_URL
        return httpx.Response(200, text=html)

    provider = EastmoneyNewsProvider(client=client_for(handler), retries=0)
    batch = await provider.fetch(window_start=START, window_end=END, cursor=None)
    assert batch.upstream_count == 1
    payload = json.loads(batch.documents[0].payload_text or "")
    assert payload["publish_time"].startswith("2026-08-30T16:31:00")
    parsed = EastmoneyNewsParser().parse(
        raw_from(provider, batch.documents[0]), provider.source
    )
    assert parsed.records[0].evidence_type is EvidenceType.NEWS
    assert parsed.records[0].source_type is EvidenceSourceType.NEWS
    assert parsed.links[0].status is EntityLinkStatus.CANDIDATE
    await provider.close()


def test_registry_orders_per_capability_and_rejects_false_declarations() -> None:
    registry = EvidenceProviderRegistry()
    cninfo = CninfoAnnouncementProvider(client=client_for(lambda _: None), retries=0)
    parser = CninfoAnnouncementParser()
    registry.register(EvidenceCapability.ANNOUNCEMENT, cninfo, parser, priority=5)
    assert registry.providers_for(EvidenceCapability.ANNOUNCEMENT) == ((cninfo, parser),)
    with pytest.raises(ValueError, match="does not declare"):
        registry.register(EvidenceCapability.NEWS, cninfo, parser)


def test_report_provider_rejects_unknown_report_before_network() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        EastmoneyReportProvider(report_name="UNKNOWN", codes=("600519",))
