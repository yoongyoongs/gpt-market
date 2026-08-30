from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

import httpx

from app.utils.time import SHANGHAI, now_shanghai
from app.v3.domain.market_data import (
    CorporateActionDraft,
    CorporateActionFetchResult,
    CorporateActionType,
    Market,
)
from app.v3.providers.corporate_actions import CorporateActionProviderError


class EastmoneyCorporateActionProvider:
    code = "eastmoney_corporate_actions"
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    report_name = "RPT_SHAREBONUS_DET"
    columns = (
        "SECUCODE,SECURITY_CODE,REPORT_DATE,NOTICE_DATE,EQUITY_RECORD_DATE,"
        "EX_DIVIDEND_DATE,ASSIGN_PROGRESS,IMPL_PLAN_PROFILE,PRETAX_BONUS_RMB,"
        "BONUS_IT_RATIO,BONUS_RATIO,IT_RATIO"
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20,
        attempts: int = 3,
        page_size: int = 500,
        max_pages: int = 200,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._attempts = attempts
        self._page_size = page_size
        self._max_pages = max_pages

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://data.eastmoney.com/",
                    "Accept": "application/json,text/plain,*/*",
                },
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def fetch_since(self, since: date) -> CorporateActionFetchResult:
        fetch_time = now_shanghai()
        first = await self._page(since, 1)
        result = first.get("result")
        if not isinstance(result, dict):
            raise CorporateActionProviderError("Eastmoney corporate action result is missing")
        pages = int(result.get("pages") or 0)
        if pages > self._max_pages:
            raise CorporateActionProviderError(
                f"Eastmoney corporate action page count {pages} exceeds limit {self._max_pages}"
            )
        rows = list(self._rows(result))
        for page in range(2, pages + 1):
            payload = await self._page(since, page)
            rows.extend(self._rows(payload.get("result")))
        actions = tuple(
            sorted(
                filter(None, (self._parse_row(row, fetch_time) for row in rows)),
                key=lambda item: (
                    item.market.value,
                    item.code,
                    item.effective_time,
                    item.source_reference,
                ),
            )
        )
        return CorporateActionFetchResult(
            source_code=self.code,
            fetch_time=fetch_time,
            actions=actions,
        )

    async def _page(self, since: date, page: int) -> dict[str, Any]:
        client = await self._get_client()
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            try:
                response = await client.get(
                    self.url,
                    params={
                        "reportName": self.report_name,
                        "columns": self.columns,
                        "filter": f"(EX_DIVIDEND_DATE>='{since.isoformat()}')",
                        "pageNumber": page,
                        "pageSize": self._page_size,
                        "sortTypes": "1,1",
                        "sortColumns": "EX_DIVIDEND_DATE,SECURITY_CODE",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("success") is not True:
                    raise ValueError("payload did not report success")
                return payload
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self._attempts:
                    await asyncio.sleep(0.25 * (2**attempt))
        raise CorporateActionProviderError(
            f"Eastmoney corporate action request failed on page {page}: {last_error}"
        ) from last_error

    @staticmethod
    def _rows(result: object) -> list[dict[str, Any]]:
        if not isinstance(result, dict):
            raise CorporateActionProviderError("Eastmoney corporate action page is malformed")
        rows = result.get("data") or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CorporateActionProviderError("Eastmoney corporate action rows are malformed")
        return rows

    def _parse_row(
        self, row: dict[str, Any], fetch_time: datetime
    ) -> CorporateActionDraft | None:
        secucode = str(row.get("SECUCODE") or "")
        code = str(row.get("SECURITY_CODE") or "")
        suffix = secucode.rsplit(".", 1)[-1]
        if len(code) != 6 or not code.isdigit() or suffix not in Market._value2member_map_:
            return None
        effective_time = self._datetime(row.get("EX_DIVIDEND_DATE"))
        report_date = self._datetime(row.get("REPORT_DATE"))
        if effective_time is None or report_date is None:
            return None
        cash = self._number(row.get("PRETAX_BONUS_RMB"))
        bonus = self._number(row.get("BONUS_RATIO"))
        capitalized = self._number(row.get("IT_RATIO"))
        has_cash = cash is not None and cash > 0
        has_stock = any(value is not None and value > 0 for value in (bonus, capitalized))
        if has_cash and has_stock:
            action_type = CorporateActionType.CASH_AND_STOCK_DISTRIBUTION
        elif has_cash:
            action_type = CorporateActionType.CASH_DIVIDEND
        elif has_stock:
            action_type = CorporateActionType.STOCK_DISTRIBUTION
        else:
            action_type = CorporateActionType.OTHER_DISTRIBUTION
        report_key = report_date.date().isoformat()
        progress = str(row.get("ASSIGN_PROGRESS") or "UNKNOWN")
        return CorporateActionDraft(
            code=code,
            market=Market(suffix),
            action_type=action_type,
            announcement_time=self._datetime(row.get("NOTICE_DATE")),
            record_time=self._datetime(row.get("EQUITY_RECORD_DATE")),
            effective_time=effective_time,
            payload={
                "report_date": report_key,
                "progress": progress,
                "effective_date_status": (
                    "CONFIRMED" if progress == "实施分配" else "PLANNED"
                ),
                "plan": row.get("IMPL_PLAN_PROFILE"),
                "cash_dividend_per_10_shares": cash,
                "bonus_shares_per_10_shares": bonus,
                "capitalized_shares_per_10_shares": capitalized,
                "combined_stock_ratio_per_10_shares": self._number(
                    row.get("BONUS_IT_RATIO")
                ),
            },
            source=self.code,
            source_reference=(
                f"eastmoney://{self.report_name}/{secucode}/{report_key}"
            ),
            fetch_time=fetch_time,
        )

    @staticmethod
    def _datetime(value: object) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=SHANGHAI)
        return parsed.astimezone(SHANGHAI)

    @staticmethod
    def _number(value: object) -> float | None:
        if value is None or value == "":
            return None
        return round(float(value), 8)
