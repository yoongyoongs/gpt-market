from __future__ import annotations

import asyncio
import json
import re
from html.parser import HTMLParser
from typing import Any

import httpx

from app.providers.base import MarketDataProvider
from app.utils.time import now_shanghai
from app.v3.domain.market_data import Market, SecurityMember, UniverseFetchResult
from app.v3.providers.universe import UniverseProviderError


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: object) -> str:
    parser = _TextExtractor()
    parser.feed(str(value or ""))
    return "".join(parser.parts).strip()


def _risk_flags(name: str) -> tuple[bool, bool]:
    normalized = name.upper().replace(" ", "")
    return "ST" in normalized, "退" in name


class LegacyUniverseProvider:
    code = "eastmoney"

    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider

    async def fetch_snapshot(self) -> UniverseFetchResult:
        total, quotes = await self._provider.get_all_a_shares()
        fetched_at = now_shanghai()
        members = []
        for quote in quotes:
            is_st, delisting_risk = _risk_flags(quote.name)
            members.append(
                SecurityMember(
                    code=quote.code,
                    market=Market(quote.market),
                    name=quote.name,
                    trading_status="SUSPENDED" if quote.suspended else "ACTIVE",
                    is_st=is_st,
                    suspended=quote.suspended,
                    delisting_risk=delisting_risk,
                    raw_reference={"snapshot_id": quote.snapshot_id, "source": quote.source},
                )
            )
        return UniverseFetchResult(
            source_code=self.code,
            as_of=max((quote.data_timestamp for quote in quotes), default=fetched_at),
            fetch_time=fetched_at,
            expected_total=max(total, len(members)),
            members=tuple(members),
        )

    async def close(self) -> None:
        return None


class OfficialUniverseWithVendorStatusProvider:
    code = "official_exchanges_enriched"

    def __init__(self, official, vendor_status) -> None:
        self._official = official
        self._vendor_status = vendor_status

    async def fetch_snapshot(self) -> UniverseFetchResult:
        official, vendor = await asyncio.gather(
            self._official.fetch_snapshot(),
            self._vendor_status.fetch_snapshot(),
            return_exceptions=True,
        )
        if isinstance(official, BaseException):
            raise official
        if isinstance(vendor, BaseException):
            return official.model_copy(update={"source_code": self.code})
        vendor_by_key = {(member.market, member.code): member for member in vendor.members}
        members = tuple(
            self._enrich(member, vendor_by_key.get((member.market, member.code)))
            for member in official.members
        )
        return official.model_copy(
            update={
                "source_code": self.code,
                "fetch_time": max(official.fetch_time, vendor.fetch_time),
                "members": members,
            }
        )

    @staticmethod
    def _enrich(official: SecurityMember, vendor: SecurityMember | None) -> SecurityMember:
        if vendor is None:
            return official
        return official.model_copy(
            update={
                "trading_status": vendor.trading_status,
                "is_st": vendor.is_st,
                "suspended": vendor.suspended,
                "delisting_risk": vendor.delisting_risk,
                "raw_reference": {
                    **official.raw_reference,
                    "status_source": "eastmoney",
                    "status_reference": vendor.raw_reference,
                },
            }
        )


class ExchangeUniverseProvider:
    code = "official_exchanges"
    sse_url = "https://query.sse.com.cn/sseQuery/commonQuery.do"
    szse_url = "https://www.szse.cn/api/report/ShowReport/data"
    bse_url = "https://www.bse.cn/nqxxController/nqxxCnzq.do"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20,
        concurrency: int = 4,
        attempts: int = 3,
        request_gap: float = 0.05,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)
        self._attempts = attempts
        self._request_gap = request_gap

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _request_json(self, url: str, params: dict[str, Any], referer: str) -> Any:
        client = await self._get_client()
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            try:
                async with self._semaphore:
                    response = await client.get(url, params=params, headers={"Referer": referer})
                    await asyncio.sleep(self._request_gap)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self._attempts:
                    await asyncio.sleep(0.25 * (2**attempt))
        raise UniverseProviderError(f"official universe request failed: {last_error}") from last_error

    async def _sse_page(self, stock_type: str, page: int) -> dict[str, Any]:
        payload = await self._request_json(
            self.sse_url,
            {
                "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
                "STOCK_TYPE": stock_type,
                "REG_PROVINCE": "",
                "CSRC_CODE": "",
                "STOCK_CODE": "",
                "COMPANY_STATUS": "2,4,5,7,8",
                "type": "inParams",
                "isPagination": "true",
                "pageHelp.cacheSize": 1,
                "pageHelp.beginPage": page,
                "pageHelp.pageSize": 500,
                "pageHelp.pageNo": page,
            },
            "https://www.sse.com.cn/assortment/stock/list/share/",
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("pageHelp"), dict):
            raise UniverseProviderError("SSE universe payload is malformed")
        return payload

    async def _szse_page(self, page: int) -> list[dict[str, Any]]:
        payload = await self._request_json(
            self.szse_url,
            {
                "SHOWTYPE": "JSON",
                "CATALOGID": "1110",
                "TABKEY": "tab1",
                "PAGENO": page,
                "random": "0.6180339887",
            },
            "https://www.szse.cn/market/stock/company/",
        )
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise UniverseProviderError("SZSE universe payload is malformed")
        return payload

    async def _bse_page(self, page: int) -> dict[str, Any]:
        client = await self._get_client()
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            try:
                async with self._semaphore:
                    response = await client.post(
                        self.bse_url,
                        data={
                            "page": str(page),
                            "typejb": "T",
                            "xxfcbj[]": "2",
                            "xxzqdm": "",
                            "sortfield": "xxzqdm",
                            "sorttype": "asc",
                        },
                        headers={"Referer": "https://www.bse.cn/nq/listedcompany.html"},
                    )
                    await asyncio.sleep(self._request_gap)
                response.raise_for_status()
                start = response.text.find("[")
                if start < 0:
                    raise ValueError("BSE universe payload has no JSON array")
                payload = json.JSONDecoder().raw_decode(response.text[start:])[0]
                if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
                    raise ValueError("BSE universe payload is malformed")
                return payload[0]
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < self._attempts:
                    await asyncio.sleep(0.25 * (2**attempt))
        raise UniverseProviderError(f"BSE universe request failed: {last_error}") from last_error

    @staticmethod
    def _parse_sse(payload: dict[str, Any]) -> list[SecurityMember]:
        rows = payload["pageHelp"].get("data") or []
        members = []
        for row in rows:
            code = str(row.get("A_STOCK_CODE") or row.get("COMPANY_CODE") or "").strip()
            name = str(row.get("SEC_NAME_CN") or row.get("COMPANY_ABBR") or "").strip()
            if not re.fullmatch(r"\d{6}", code) or not name:
                continue
            is_st, delisting_risk = _risk_flags(name)
            members.append(
                SecurityMember(
                    code=code,
                    market=Market.SH,
                    name=name,
                    trading_status="ACTIVE",
                    is_st=is_st,
                    delisting_risk=delisting_risk,
                    raw_reference={"exchange": "SSE", "state_code": row.get("STATE_CODE_STOCK")},
                )
            )
        return members

    @staticmethod
    def _parse_szse(payload: list[dict[str, Any]]) -> list[SecurityMember]:
        rows = payload[0].get("data") or []
        members = []
        for row in rows:
            code = str(row.get("agdm") or "").strip()
            name = _plain_text(row.get("agjc"))
            if not re.fullmatch(r"\d{6}", code) or not name:
                continue
            is_st, delisting_risk = _risk_flags(name)
            members.append(
                SecurityMember(
                    code=code,
                    market=Market.SZ,
                    name=name,
                    trading_status="ACTIVE",
                    is_st=is_st,
                    delisting_risk=delisting_risk,
                    raw_reference={"exchange": "SZSE", "board": row.get("bk")},
                )
            )
        return members

    @staticmethod
    def _parse_bse(payload: dict[str, Any]) -> list[SecurityMember]:
        members = []
        for row in payload.get("content") or []:
            code = str(row.get("xxzqdm") or "").strip()
            name = str(row.get("xxzqjc") or "").strip()
            if not re.fullmatch(r"920\d{3}", code) or not name:
                continue
            is_st, delisting_risk = _risk_flags(name)
            members.append(
                SecurityMember(
                    code=code,
                    market=Market.BJ,
                    name=name,
                    trading_status="ACTIVE",
                    is_st=is_st,
                    delisting_risk=delisting_risk,
                    raw_reference={
                        "exchange": "BSE",
                        "listing_date": row.get("xxgprq"),
                        "industry": row.get("xxhyzl"),
                    },
                )
            )
        return members

    async def fetch_snapshot(self) -> UniverseFetchResult:
        fetched_at = now_shanghai()
        sse_main, sse_star, szse_first, bse_first = await asyncio.gather(
            self._sse_page("1", 1),
            self._sse_page("8", 1),
            self._szse_page(1),
            self._bse_page(0),
        )
        sse_payloads = [sse_main, sse_star]
        sse_requests = []
        for stock_type, first in (("1", sse_main), ("8", sse_star)):
            page_count = int(first["pageHelp"].get("pageCount") or 1)
            sse_requests.extend(self._sse_page(stock_type, page) for page in range(2, page_count + 1))
        szse_metadata = szse_first[0].get("metadata") or {}
        szse_page_count = int(szse_metadata.get("pagecount") or 1)
        bse_page_count = int(bse_first.get("totalPages") or 1)
        remaining, szse_remaining, bse_remaining = await asyncio.gather(
            asyncio.gather(*sse_requests),
            asyncio.gather(*(self._szse_page(page) for page in range(2, szse_page_count + 1))),
            asyncio.gather(*(self._bse_page(page) for page in range(1, bse_page_count))),
        )
        sse_payloads.extend(remaining)
        members = [member for payload in sse_payloads for member in self._parse_sse(payload)]
        szse_payloads = [szse_first, *szse_remaining]
        szse_members = [member for payload in szse_payloads for member in self._parse_szse(payload)]
        szse_expected = int(szse_metadata.get("recordcount") or 0)
        if szse_expected and len(szse_members) != szse_expected:
            raise UniverseProviderError(
                f"SZSE universe incomplete: expected {szse_expected}, parsed {len(szse_members)}"
            )
        members.extend(szse_members)
        bse_payloads = [bse_first, *bse_remaining]
        bse_members = [member for payload in bse_payloads for member in self._parse_bse(payload)]
        bse_expected = int(bse_first.get("totalElements") or 0)
        if bse_expected and len(bse_members) != bse_expected:
            raise UniverseProviderError(
                f"BSE universe incomplete: expected {bse_expected}, parsed {len(bse_members)}"
            )
        members.extend(bse_members)
        expected_total = sum(int(payload["pageHelp"].get("total") or 0) for payload in (sse_main, sse_star))
        expected_total += max(szse_expected, len(szse_members))
        expected_total += max(bse_expected, len(bse_members))
        return UniverseFetchResult(
            source_code=self.code,
            as_of=fetched_at,
            fetch_time=fetched_at,
            expected_total=max(expected_total, len(members)),
            members=tuple(members),
        )
