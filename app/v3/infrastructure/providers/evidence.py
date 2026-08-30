from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from time import monotonic
from typing import Any
from urllib.parse import urlencode

import httpx

from app.providers.symbols import market_of, validate_code
from app.utils.time import SHANGHAI, now_shanghai
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import (
    DecayModel,
    EntityLink,
    EntityLinkStatus,
    EvidenceSource,
    EvidenceSourceType,
    FetchedDocument,
    NormalizedEvidence,
    RawDocument,
)
from app.v3.domain.hashing import canonical_hash, canonical_json
from app.v3.providers.evidence import (
    EvidenceFetchBatch,
    EvidenceCapability,
    EvidenceParser,
    EvidenceProvider,
    ParsedEvidenceBundle,
)


CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_URL = "https://static.cninfo.com.cn/"
SSE_ANNOUNCEMENT_URL = "https://query.sse.com.cn/security/stock/queryCompanyBulletinNew.do"
SSE_STATIC_URL = "https://static.sse.com.cn"
EASTMONEY_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_NEWS_URL = "https://finance.eastmoney.com/yaowen.html"
GOV_POLICY_URL = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"


class EvidenceProviderError(RuntimeError):
    pass


class AsyncRequestGate:
    def __init__(
        self,
        *,
        rate_limit_per_minute: int | None,
        retries: int = 2,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if retries < 0:
            raise ValueError("retries cannot be negative")
        self._interval = 0.0 if rate_limit_per_minute is None else 60 / rate_limit_per_minute
        self._retries = retries
        self._sleeper = sleeper
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_request_at: float | None = None

    async def request(self, operation: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
        for attempt in range(self._retries + 1):
            await self._wait_for_slot()
            try:
                response = await operation()
                if response.status_code == 429 or response.status_code >= 500:
                    raise EvidenceProviderError(f"retryable upstream HTTP {response.status_code}")
                response.raise_for_status()
                return response
            except (httpx.TransportError, EvidenceProviderError) as exc:
                if attempt == self._retries:
                    raise EvidenceProviderError(str(exc)) from exc
                await self._sleeper(min(2**attempt, 4))
        raise AssertionError("request retry loop exited unexpectedly")

    async def _wait_for_slot(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last_request_at is not None:
                delay = self._interval - (now - self._last_request_at)
                if delay > 0:
                    await self._sleeper(delay)
            self._last_request_at = self._clock()


class _HttpEvidenceProvider:
    source: EvidenceSource

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None,
        timeout: float,
        retries: int,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": "gpt-market-v3-evidence/1.0"},
        )
        self._gate = AsyncRequestGate(
            rate_limit_per_minute=self.source.rate_limit_per_minute,
            retries=retries,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._gate.request(lambda: self._client.get(url, **kwargs))

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self._gate.request(lambda: self._client.post(url, **kwargs))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _json_payload(value: object) -> str:
    return canonical_json(value)


def _window_dates(
    window_start: datetime | None, window_end: datetime | None
) -> tuple[datetime, datetime]:
    end = window_end or now_shanghai()
    start = window_start or end - timedelta(days=1)
    if start > end:
        raise ValueError("evidence window_start cannot be after window_end")
    return start.astimezone(SHANGHAI), end.astimezone(SHANGHAI)


def _security_subject(code: object) -> str:
    normalized = validate_code(str(code))
    if not _is_a_share_code(normalized):
        raise ValueError(f"not an A-share security code: {normalized}")
    return f"{market_of(normalized)}:{normalized}"


def _is_a_share_code(code: object) -> bool:
    try:
        normalized = validate_code(str(code))
    except ValueError:
        return False
    return normalized.startswith(
        ("6", "000", "001", "002", "003", "300", "301", "4", "8", "920")
    )


def _parse_datetime(value: object) -> datetime | None:
    if value in (None, "", "-"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed


def _announcement_claim_key(code: object, title: object, published: datetime) -> str:
    title_hash = canonical_hash(str(title).strip())[:16]
    return f"announcement:{validate_code(str(code))}:{published:%Y-%m-%d}:{title_hash}"


class CninfoAnnouncementProvider(_HttpEvidenceProvider):
    source = EvidenceSource(
        code="cninfo-announcements",
        source_type=EvidenceSourceType.OFFICIAL,
        upstream_source="cninfo",
        capabilities={"types": [EvidenceCapability.ANNOUNCEMENT]},
        priority=10,
        rate_limit_per_minute=30,
        parser_version="cninfo-v1",
        reliability=0.98,
    )

    def __init__(
        self,
        *,
        page_size: int = 30,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15,
        retries: int = 2,
    ) -> None:
        if not 1 <= page_size <= 30:
            raise ValueError("CNINFO page_size must be between 1 and 30")
        self._page_size = page_size
        super().__init__(client=client, timeout=timeout, retries=retries)

    async def fetch(
        self,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
        cursor: dict[str, object] | None,
    ) -> EvidenceFetchBatch:
        start, end = _window_dates(window_start, window_end)
        page = int((cursor or {}).get("page", 1))
        if page < 1:
            raise ValueError("CNINFO cursor page must be positive")
        response = await self._post(
            CNINFO_QUERY_URL,
            data={
                "pageNum": page,
                "pageSize": self._page_size,
                "column": "",
                "tabName": "fulltext",
                "plate": "",
                "stock": "",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{start:%Y-%m-%d}~{end:%Y-%m-%d}",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
            headers={
                "Origin": "https://www.cninfo.com.cn",
                "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        try:
            payload = response.json()
            rows = payload["announcements"] or []
            total = int(payload.get("totalRecordNum") or payload.get("totalAnnouncement") or 0)
        except (TypeError, ValueError, KeyError) as exc:
            raise EvidenceProviderError("CNINFO response shape is invalid") from exc
        fetch_time = _now_utc()
        documents = tuple(
            FetchedDocument(
                document_key=f"announcement:{row['announcementId']}",
                raw_reference=f"{CNINFO_STATIC_URL}{str(row['adjunctUrl']).lstrip('/')}",
                mime_type="application/json",
                payload_text=_json_payload(row),
                response_metadata={
                    "list_url": CNINFO_QUERY_URL,
                    "http_status": response.status_code,
                    "page": page,
                },
                fetch_time=fetch_time,
                known_at=fetch_time,
            )
            for row in rows
            if row.get("announcementId")
            and row.get("adjunctUrl")
            and _is_a_share_code(row.get("secCode"))
        )
        exhausted = len(rows) < self._page_size or page * self._page_size >= total
        return EvidenceFetchBatch(
            documents=documents,
            next_cursor=None if exhausted else {"page": page + 1},
            exhausted=exhausted,
            upstream_count=total,
        )


class CninfoAnnouncementParser:
    code = "cninfo-announcement"
    version = "cninfo-v1"

    def parse(self, raw: RawDocument, source: EvidenceSource) -> ParsedEvidenceBundle:
        row = json.loads(raw.payload_text or "")
        subject_id = _security_subject(row["secCode"])
        published = datetime.fromtimestamp(int(row["announcementTime"]) / 1000, tz=SHANGHAI)
        normalized = {
            "security_code": str(row["secCode"]),
            "title": str(row["announcementTitle"]).strip(),
            "publish_date": f"{published:%Y-%m-%d}",
        }
        record = NormalizedEvidence.build(
            raw_document_id=raw.raw_document_id,
            evidence_type=EvidenceType.OFFICIAL_DISCLOSURE,
            source_type=source.source_type,
            source_priority=source.priority,
            subject_type="SECURITY",
            subject_id=subject_id,
            claim_key=_announcement_claim_key(
                row["secCode"], row["announcementTitle"], published
            ),
            source=source.code,
            upstream_source=source.upstream_source,
            payload=row,
            normalized_payload=normalized,
            event_time=None,
            publish_time=published,
            fetch_time=raw.fetch_time,
            known_at=raw.known_at,
            confidence=source.reliability,
            relevance=0.9,
            decay_model=DecayModel.NONE,
            parser_version=self.version,
        )
        link = EntityLink.build(
            evidence_id=record.evidence_id,
            entity_type="SECURITY",
            entity_id=subject_id,
            match_basis={"field": "secCode", "value": row["secCode"], "source": "official"},
            confidence=1,
            status=EntityLinkStatus.CONFIRMED,
        )
        return ParsedEvidenceBundle(records=(record,), links=(link,))


class SseAnnouncementProvider(_HttpEvidenceProvider):
    source = EvidenceSource(
        code="sse-announcements",
        source_type=EvidenceSourceType.OFFICIAL,
        upstream_source="sse",
        capabilities={"types": [EvidenceCapability.ANNOUNCEMENT]},
        priority=20,
        rate_limit_per_minute=30,
        parser_version="sse-announcement-v1",
        reliability=0.98,
    )

    def __init__(
        self,
        *,
        page_size: int = 25,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15,
        retries: int = 2,
    ) -> None:
        if not 1 <= page_size <= 100:
            raise ValueError("SSE page_size must be between 1 and 100")
        self._page_size = page_size
        super().__init__(client=client, timeout=timeout, retries=retries)

    async def fetch(
        self,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
        cursor: dict[str, object] | None,
    ) -> EvidenceFetchBatch:
        start, end = _window_dates(window_start, window_end)
        page = int((cursor or {}).get("page", 1))
        if page < 1:
            raise ValueError("SSE cursor page must be positive")
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": self._page_size,
            "pageHelp.pageNo": page,
            "pageHelp.cacheSize": 1,
            "START_DATE": f"{start:%Y-%m-%d}",
            "END_DATE": f"{end:%Y-%m-%d}",
            "SECURITY_CODE": "",
            "TITLE": "",
            "BULLETIN_TYPE": "",
            "stockType": "",
        }
        response = await self._get(
            SSE_ANNOUNCEMENT_URL,
            params=params,
            headers={"Referer": "https://www.sse.com.cn/disclosure/listedinfo/announcement/"},
        )
        try:
            payload = response.json()
            grouped_rows = payload["result"] or []
            page_help = payload["pageHelp"]
            total = int(page_help["total"])
            page_count = int(page_help["pageCount"])
        except (TypeError, ValueError, KeyError) as exc:
            raise EvidenceProviderError("SSE announcement response shape is invalid") from exc
        rows = [row for group in grouped_rows for row in group if row.get("URL")]
        fetch_time = _now_utc()
        documents = tuple(
            FetchedDocument(
                document_key=f"sse:{row['ORG_BULLETIN_ID']}:{str(row['URL']).rsplit('/', 1)[-1]}",
                raw_reference=f"{SSE_STATIC_URL}{row['URL']}",
                mime_type="application/json",
                payload_text=_json_payload(row),
                response_metadata={
                    "list_url": SSE_ANNOUNCEMENT_URL,
                    "http_status": response.status_code,
                    "page": page,
                },
                fetch_time=fetch_time,
                known_at=fetch_time,
            )
            for row in rows
            if row.get("ORG_BULLETIN_ID") and row.get("SECURITY_CODE")
        )
        exhausted = page >= page_count
        return EvidenceFetchBatch(
            documents=documents,
            next_cursor=None if exhausted else {"page": page + 1},
            exhausted=exhausted,
            upstream_count=total,
        )


class SseAnnouncementParser:
    code = "sse-announcement"
    version = "sse-announcement-v1"

    def parse(self, raw: RawDocument, source: EvidenceSource) -> ParsedEvidenceBundle:
        row = json.loads(raw.payload_text or "")
        published = _parse_datetime(row["SSEDATE"])
        if published is None:
            raise ValueError("SSE announcement has no valid publish date")
        subject_id = _security_subject(row["SECURITY_CODE"])
        normalized = {
            "security_code": str(row["SECURITY_CODE"]),
            "title": str(row["TITLE"]).strip(),
            "publish_date": f"{published:%Y-%m-%d}",
        }
        record = NormalizedEvidence.build(
            raw_document_id=raw.raw_document_id,
            evidence_type=EvidenceType.OFFICIAL_DISCLOSURE,
            source_type=source.source_type,
            source_priority=source.priority,
            subject_type="SECURITY",
            subject_id=subject_id,
            claim_key=_announcement_claim_key(
                row["SECURITY_CODE"], row["TITLE"], published
            ),
            source=source.code,
            upstream_source=source.upstream_source,
            payload=row,
            normalized_payload=normalized,
            event_time=None,
            publish_time=published,
            fetch_time=raw.fetch_time,
            known_at=raw.known_at,
            confidence=source.reliability,
            relevance=0.9,
            decay_model=DecayModel.NONE,
            parser_version=self.version,
        )
        link = EntityLink.build(
            evidence_id=record.evidence_id,
            entity_type="SECURITY",
            entity_id=subject_id,
            match_basis={
                "field": "SECURITY_CODE",
                "value": row["SECURITY_CODE"],
                "source": "official",
            },
            confidence=1,
            status=EntityLinkStatus.CONFIRMED,
        )
        return ParsedEvidenceBundle(records=(record,), links=(link,))


class EastmoneyReportProvider(_HttpEvidenceProvider):
    REPORT_CAPABILITIES = {
        "RPT_F10_FINANCE_MAINFINADATA": EvidenceCapability.FINANCIAL,
        "RPT_PUBLIC_OP_NEWPREDICT": EvidenceCapability.PERFORMANCE,
        "RPT_FCI_PERFORMANCEE": EvidenceCapability.PERFORMANCE,
    }

    def __init__(
        self,
        *,
        report_name: str,
        codes: Iterable[str],
        chunk_size: int = 20,
        rows_per_code: int = 8,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15,
        retries: int = 2,
    ) -> None:
        if report_name not in self.REPORT_CAPABILITIES:
            raise ValueError("unsupported Eastmoney evidence report")
        if not 1 <= chunk_size <= 50:
            raise ValueError("Eastmoney chunk_size must be between 1 and 50")
        self._report_name = report_name
        self._codes = tuple(dict.fromkeys(validate_code(code) for code in codes))
        self._chunk_size = chunk_size
        self._rows_per_code = rows_per_code
        capability = self.REPORT_CAPABILITIES[report_name]
        self.source = EvidenceSource(
            code=f"eastmoney-{report_name.lower()}",
            source_type=EvidenceSourceType.VENDOR,
            upstream_source="eastmoney-datacenter",
            capabilities={"types": [capability], "report_name": report_name},
            priority=50,
            rate_limit_per_minute=60,
            parser_version="eastmoney-report-v1",
            reliability=0.8,
        )
        super().__init__(client=client, timeout=timeout, retries=retries)

    async def fetch(
        self,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
        cursor: dict[str, object] | None,
    ) -> EvidenceFetchBatch:
        del window_start, window_end
        offset = int((cursor or {}).get("code_offset", 0))
        if offset < 0 or offset > len(self._codes):
            raise ValueError("Eastmoney code_offset is outside the configured code set")
        codes = self._codes[offset : offset + self._chunk_size]
        if not codes:
            return EvidenceFetchBatch(documents=(), exhausted=True, upstream_count=0)
        expression = ",".join(f'"{code}"' for code in codes)
        params = {
            "reportName": self._report_name,
            "columns": "ALL",
            "filter": f"(SECURITY_CODE in ({expression}))",
            "pageNumber": 1,
            "pageSize": max(20, len(codes) * self._rows_per_code),
            "sortTypes": -1,
            "sortColumns": "REPORT_DATE",
        }
        response = await self._get(EASTMONEY_DATACENTER_URL, params=params)
        try:
            payload = response.json()
            if not payload.get("success") and payload.get("result") is None:
                raise EvidenceProviderError(f"Eastmoney report failed: {payload.get('message')}")
            result = payload.get("result") or {}
            rows = result.get("data") or []
        except (TypeError, ValueError) as exc:
            raise EvidenceProviderError("Eastmoney report response shape is invalid") from exc
        fetch_time = _now_utc()
        reference = f"{EASTMONEY_DATACENTER_URL}?{urlencode(params)}"
        documents = tuple(
            FetchedDocument(
                document_key=(
                    f"{self._report_name}:{row.get('SECURITY_CODE')}:"
                    f"{str(row.get('REPORT_DATE') or '')[:10]}"
                ),
                raw_reference=reference,
                mime_type="application/json",
                payload_text=_json_payload({"report_name": self._report_name, "row": row}),
                response_metadata={"http_status": response.status_code, "code_offset": offset},
                fetch_time=fetch_time,
                known_at=fetch_time,
            )
            for row in rows
            if row.get("SECURITY_CODE") and row.get("REPORT_DATE")
        )
        next_offset = offset + len(codes)
        exhausted = next_offset >= len(self._codes)
        return EvidenceFetchBatch(
            documents=documents,
            next_cursor=None if exhausted else {"code_offset": next_offset},
            exhausted=exhausted,
        )


class EastmoneyReportParser:
    code = "eastmoney-report"
    version = "eastmoney-report-v1"
    FINANCIAL_FIELDS = (
        "TOTALOPERATEREVE",
        "PARENTNETPROFIT",
        "KCFJCXSYJLR",
        "NETCASH_OPERATE_PK",
        "ROEJQ",
        "XSMLL",
        "ZCFZL",
    )
    FORECAST_FIELDS = (
        "PREDICT_TYPE",
        "PREDICT_AMT_LOWER",
        "PREDICT_AMT_UPPER",
        "ADD_AMP_LOWER",
        "ADD_AMP_UPPER",
        "CHANGE_REASON_EXPLAIN",
    )
    EXPRESS_FIELDS = (
        "TOTAL_OPERATE_INCOME",
        "YSTZ",
        "PARENT_NETPROFIT",
        "JLRTBZCL",
        "WEIGHTAVG_ROE",
    )

    def parse(self, raw: RawDocument, source: EvidenceSource) -> ParsedEvidenceBundle:
        payload = json.loads(raw.payload_text or "")
        report_name = str(payload["report_name"])
        row = payload["row"]
        code = validate_code(str(row["SECURITY_CODE"]))
        subject_id = _security_subject(code)
        period = str(row["REPORT_DATE"])[:10]
        fields = self._fields_for(report_name)
        normalized = {
            "report_name": report_name,
            "security_code": code,
            "report_period": period,
            "values": {field: row.get(field) for field in fields},
        }
        record = NormalizedEvidence.build(
            raw_document_id=raw.raw_document_id,
            evidence_type=EvidenceType.VENDOR_DATA,
            source_type=source.source_type,
            source_priority=source.priority,
            subject_type="SECURITY",
            subject_id=subject_id,
            claim_key=f"{report_name}:{period}",
            source=source.code,
            upstream_source=source.upstream_source,
            payload=row,
            normalized_payload=normalized,
            event_time=_parse_datetime(row.get("REPORT_DATE")),
            publish_time=_parse_datetime(row.get("NOTICE_DATE")),
            fetch_time=raw.fetch_time,
            known_at=raw.known_at,
            confidence=source.reliability,
            relevance=0.85,
            decay_model=DecayModel.NONE,
            parser_version=self.version,
        )
        link = EntityLink.build(
            evidence_id=record.evidence_id,
            entity_type="SECURITY",
            entity_id=subject_id,
            match_basis={"field": "SECURITY_CODE", "value": code, "source": "vendor"},
            confidence=1,
            status=EntityLinkStatus.CONFIRMED,
        )
        return ParsedEvidenceBundle(records=(record,), links=(link,))

    def _fields_for(self, report_name: str) -> tuple[str, ...]:
        if report_name == "RPT_F10_FINANCE_MAINFINADATA":
            return self.FINANCIAL_FIELDS
        if report_name == "RPT_PUBLIC_OP_NEWPREDICT":
            return self.FORECAST_FIELDS
        if report_name == "RPT_FCI_PERFORMANCEE":
            return self.EXPRESS_FIELDS
        raise ValueError("unsupported Eastmoney report payload")


class GovernmentPolicyProvider(_HttpEvidenceProvider):
    source = EvidenceSource(
        code="gov-cn-latest-policy",
        source_type=EvidenceSourceType.OFFICIAL,
        upstream_source="www.gov.cn",
        capabilities={"types": [EvidenceCapability.POLICY]},
        priority=20,
        rate_limit_per_minute=20,
        parser_version="gov-policy-v1",
        reliability=0.98,
    )

    def __init__(
        self,
        *,
        page_size: int = 20,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15,
        retries: int = 2,
    ) -> None:
        if page_size < 1:
            raise ValueError("policy page_size must be positive")
        self._page_size = page_size
        super().__init__(client=client, timeout=timeout, retries=retries)

    async def fetch(
        self,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
        cursor: dict[str, object] | None,
    ) -> EvidenceFetchBatch:
        start, end = _window_dates(window_start, window_end)
        offset = int((cursor or {}).get("offset", 0))
        response = await self._get(GOV_POLICY_URL)
        try:
            rows = response.json()
            if not isinstance(rows, list):
                raise TypeError
        except (TypeError, ValueError) as exc:
            raise EvidenceProviderError("government policy response shape is invalid") from exc
        selected = [
            row
            for row in rows
            if (published := _parse_datetime(row.get("DOCRELPUBTIME"))) is not None
            and start.date() <= published.astimezone(SHANGHAI).date() <= end.date()
        ]
        page = selected[offset : offset + self._page_size]
        fetch_time = _now_utc()
        documents = tuple(
            FetchedDocument(
                document_key=f"policy:{row['URL']}",
                raw_reference=str(row["URL"]),
                mime_type="application/json",
                payload_text=_json_payload(row),
                response_metadata={"list_url": GOV_POLICY_URL, "http_status": response.status_code},
                fetch_time=fetch_time,
                known_at=fetch_time,
            )
            for row in page
            if row.get("URL") and row.get("TITLE")
        )
        next_offset = offset + len(page)
        exhausted = next_offset >= len(selected)
        return EvidenceFetchBatch(
            documents=documents,
            next_cursor=None if exhausted else {"offset": next_offset},
            exhausted=exhausted,
            upstream_count=len(selected),
        )


class GovernmentPolicyParser:
    code = "gov-policy"
    version = "gov-policy-v1"

    def parse(self, raw: RawDocument, source: EvidenceSource) -> ParsedEvidenceBundle:
        row = json.loads(raw.payload_text or "")
        published = _parse_datetime(row["DOCRELPUBTIME"])
        normalized = {
            "title": str(row["TITLE"]).strip(),
            "url": str(row["URL"]),
            "publish_date": str(row["DOCRELPUBTIME"]),
        }
        record = NormalizedEvidence.build(
            raw_document_id=raw.raw_document_id,
            evidence_type=EvidenceType.FACT,
            source_type=source.source_type,
            source_priority=source.priority,
            subject_type="MARKET",
            subject_id="CN_A_SHARES",
            claim_key=f"policy:{row['URL']}",
            source=source.code,
            upstream_source=source.upstream_source,
            payload=row,
            normalized_payload=normalized,
            event_time=published,
            publish_time=published,
            fetch_time=raw.fetch_time,
            known_at=raw.known_at,
            confidence=source.reliability,
            relevance=0.75,
            decay_model=DecayModel.EXPONENTIAL,
            decay_rate=0.01,
            parser_version=self.version,
        )
        link = EntityLink.build(
            evidence_id=record.evidence_id,
            entity_type="MARKET",
            entity_id="CN_A_SHARES",
            match_basis={"field": "publisher", "value": "www.gov.cn"},
            confidence=1,
            status=EntityLinkStatus.CONFIRMED,
        )
        return ParsedEvidenceBundle(records=(record,), links=(link,))


class _EastmoneyNewsHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, str]] = []
        self._capture: str | None = None
        self._current: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "p" and "title" in classes:
            self._current = {}
            self._capture = "title-container"
        elif tag == "a" and self._capture == "title-container":
            href = attributes.get("href") or ""
            if re.fullmatch(r"https?://finance\.eastmoney\.com/a/\d+\.html", href):
                self._current["url"] = href
                self._capture = "title"
        elif tag == "p" and "time" in classes and self._current.get("url"):
            self._capture = "time"

    def handle_data(self, data: str) -> None:
        if self._capture in {"title", "time"}:
            key = self._capture
            self._current[key] = f"{self._current.get(key, '')}{data}".strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture == "title":
            self._capture = "title-container"
        elif tag == "p" and self._capture == "time":
            if self._current.get("title") and self._current.get("time"):
                self.items.append(self._current)
            self._current = {}
            self._capture = None
        elif tag == "p" and self._capture == "title-container":
            self._capture = None


class EastmoneyNewsProvider(_HttpEvidenceProvider):
    source = EvidenceSource(
        code="eastmoney-finance-news",
        source_type=EvidenceSourceType.NEWS,
        upstream_source="finance.eastmoney.com",
        capabilities={"types": [EvidenceCapability.NEWS]},
        priority=70,
        rate_limit_per_minute=20,
        parser_version="eastmoney-news-v1",
        reliability=0.65,
    )

    def __init__(
        self,
        *,
        page_size: int = 30,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15,
        retries: int = 2,
    ) -> None:
        if page_size < 1:
            raise ValueError("news page_size must be positive")
        self._page_size = page_size
        super().__init__(client=client, timeout=timeout, retries=retries)

    async def fetch(
        self,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
        cursor: dict[str, object] | None,
    ) -> EvidenceFetchBatch:
        start, end = _window_dates(window_start, window_end)
        offset = int((cursor or {}).get("offset", 0))
        response = await self._get(EASTMONEY_NEWS_URL)
        parser = _EastmoneyNewsHTMLParser()
        parser.feed(response.text)
        rows = []
        for item in parser.items:
            published = self._published_at(item)
            if start <= published <= end:
                rows.append({**item, "publish_time": published.isoformat()})
        page = rows[offset : offset + self._page_size]
        fetch_time = _now_utc()
        documents = tuple(
            FetchedDocument(
                document_key=f"news:{row['url'].rsplit('/', 1)[-1].removesuffix('.html')}",
                raw_reference=row["url"],
                mime_type="application/json",
                payload_text=_json_payload(row),
                response_metadata={"list_url": EASTMONEY_NEWS_URL, "http_status": response.status_code},
                fetch_time=fetch_time,
                known_at=fetch_time,
            )
            for row in page
        )
        next_offset = offset + len(page)
        exhausted = next_offset >= len(rows)
        return EvidenceFetchBatch(
            documents=documents,
            next_cursor=None if exhausted else {"offset": next_offset},
            exhausted=exhausted,
            upstream_count=len(rows),
        )

    @staticmethod
    def _published_at(item: dict[str, str]) -> datetime:
        match = re.search(r"/a/(\d{8})\d+\.html", item["url"])
        time_match = re.fullmatch(r"\d{1,2}月\d{1,2}日\s+(\d{1,2}):(\d{2})", item["time"])
        if match is None or time_match is None:
            raise EvidenceProviderError("Eastmoney news timestamp is invalid")
        day = datetime.strptime(match.group(1), "%Y%m%d")
        return day.replace(
            hour=int(time_match.group(1)),
            minute=int(time_match.group(2)),
            tzinfo=SHANGHAI,
        )


class EastmoneyNewsParser:
    code = "eastmoney-news"
    version = "eastmoney-news-v1"

    def parse(self, raw: RawDocument, source: EvidenceSource) -> ParsedEvidenceBundle:
        row = json.loads(raw.payload_text or "")
        published = _parse_datetime(row["publish_time"])
        normalized = {
            "title": str(row["title"]).strip(),
            "url": str(row["url"]),
            "publish_time": published.isoformat() if published else None,
        }
        record = NormalizedEvidence.build(
            raw_document_id=raw.raw_document_id,
            evidence_type=EvidenceType.NEWS,
            source_type=source.source_type,
            source_priority=source.priority,
            subject_type="MARKET",
            subject_id="CN_A_SHARES",
            claim_key=f"news:{row['url']}",
            source=source.code,
            upstream_source=source.upstream_source,
            payload=row,
            normalized_payload=normalized,
            event_time=published,
            publish_time=published,
            fetch_time=raw.fetch_time,
            known_at=raw.known_at,
            confidence=source.reliability,
            relevance=0.65,
            decay_model=DecayModel.EXPONENTIAL,
            decay_rate=0.2,
            parser_version=self.version,
        )
        link = EntityLink.build(
            evidence_id=record.evidence_id,
            entity_type="MARKET",
            entity_id="CN_A_SHARES",
            match_basis={"field": "default_scope", "value": "market_news"},
            confidence=0.6,
            status=EntityLinkStatus.CANDIDATE,
        )
        return ParsedEvidenceBundle(records=(record,), links=(link,))


class EvidenceProviderRegistry:
    def __init__(self) -> None:
        self._bindings: dict[
            EvidenceCapability, list[tuple[int, EvidenceProvider, EvidenceParser]]
        ] = {}

    def register(
        self,
        capability: EvidenceCapability,
        provider: EvidenceProvider,
        parser: EvidenceParser,
        *,
        priority: int | None = None,
    ) -> None:
        configured = {str(item) for item in provider.source.capabilities.get("types", [])}
        if capability not in configured:
            raise ValueError("provider does not declare the registered evidence capability")
        if parser.version != provider.source.parser_version:
            raise ValueError("provider and parser versions do not match")
        bindings = self._bindings.setdefault(capability, [])
        if any(item[1].source.code == provider.source.code for item in bindings):
            raise ValueError("evidence provider is already registered for this capability")
        bindings.append((priority or provider.source.priority, provider, parser))
        bindings.sort(key=lambda item: (item[0], item[1].source.code))

    def providers_for(
        self, capability: EvidenceCapability
    ) -> tuple[tuple[EvidenceProvider, EvidenceParser], ...]:
        return tuple((provider, parser) for _, provider, parser in self._bindings.get(capability, []))

    async def close(self) -> None:
        closed: set[int] = set()
        for bindings in self._bindings.values():
            for _, provider, _ in bindings:
                if id(provider) not in closed:
                    closed.add(id(provider))
                    await provider.close()
