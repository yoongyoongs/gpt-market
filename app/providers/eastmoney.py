from __future__ import annotations

import asyncio
import json
import re
from math import ceil
from datetime import datetime
from typing import Any

import httpx

from app.cache import AsyncTTLCache
from app.config import Settings
from app.models import Kline, KlineResult, Quote, SectorItem, SectorRanking
from app.providers.base import MarketDataProvider, ProviderError
from app.services.data_quality import DataQualityService, default_data_quality_service
from app.utils.time import SHANGHAI, now_shanghai

QUOTE_FIELDS = "f57,f58,f43,f60,f46,f44,f45,f170,f169,f47,f48,f168,f50,f171,f86"
LIST_FIELDS = "f12,f14,f2,f18,f17,f15,f16,f3,f4,f5,f6,f8,f10,f7,f13,f124"
SECTOR_FIELDS = "f12,f14,f3,f6,f104,f105,f124"
QUOTE_UT = "fa5fd1943c7b386f172d6893dbfba10b"
LIST_UT = "bd1d9ddb04089700cf9c27f6f7426281"
PERIOD_MAP = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "day": 101, "week": 102, "month": 103}
MAINBOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")


def validate_code(code: str) -> str:
    value = code.strip()
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("code must contain exactly 6 digits")
    return value


def market_of(code: str) -> str:
    code = validate_code(code)
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("0", "1", "2", "3")):
        return "SZ"
    raise ValueError(f"unsupported security code: {code}")


def to_eastmoney_secid(code: str) -> str:
    code = validate_code(code)
    market = market_of(code)
    return f"{1 if market == 'SH' else 0}.{code}"


def scale_raw(value: Any, divisor: float = 100.0) -> float | None:
    if value in (None, "-", ""):
        return None
    return round(float(value) / divisor, 6)


def _number(value: Any) -> float | None:
    if value in (None, "-", ""):
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _timestamp(value: Any, fetch_timestamp: datetime) -> tuple[datetime, str]:
    if value not in (None, "-", ""):
        try:
            return datetime.fromtimestamp(int(value), tz=SHANGHAI), "eastmoney"
        except (ValueError, TypeError, OSError):
            pass
    return fetch_timestamp, "fetch_time"


def parse_quote(
    data: dict[str, Any],
    *,
    fltt: int = 2,
    fetched_at: datetime | None = None,
    quality_service: DataQualityService = default_data_quality_service,
) -> Quote:
    fetch_timestamp = fetched_at or now_shanghai()
    code = str(data.get("f57") or data.get("f12") or "")
    if not code:
        raise ProviderError("eastmoney quote is missing code")
    divisor = 1.0 if fltt == 2 else 100.0
    price = scale_raw(data.get("f43", data.get("f2")), divisor)
    prev_close = scale_raw(data.get("f60", data.get("f18")), divisor)
    open_price = scale_raw(data.get("f46", data.get("f17")), divisor)
    high = scale_raw(data.get("f44", data.get("f15")), divisor)
    low = scale_raw(data.get("f45", data.get("f16")), divisor)
    pct = scale_raw(data.get("f170", data.get("f3")), divisor)
    change = scale_raw(data.get("f169", data.get("f4")), divisor)
    turnover = scale_raw(data.get("f168", data.get("f8")), divisor)
    volume_ratio = scale_raw(data.get("f50", data.get("f10")), divisor)
    amplitude = scale_raw(data.get("f171", data.get("f7")), divisor)
    ts, timestamp_source = _timestamp(data.get("f86", data.get("f124")), fetch_timestamp)
    volume_lots = _integer(data.get("f47", data.get("f5")))
    complete = all(value is not None for value in (price, prev_close, open_price, high, low))
    return Quote(
        code=code,
        name=str(data.get("f58") or data.get("f14") or ""),
        market=market_of(code),
        price=price,
        prev_close=prev_close,
        open=open_price,
        high=high,
        low=low,
        pct_change=pct,
        change=change,
        volume=None if volume_lots is None else volume_lots * 100,
        amount=_number(data.get("f48", data.get("f6"))),
        turnover_rate=turnover,
        volume_ratio=volume_ratio,
        amplitude=amplitude,
        suspended=price is None or volume_lots in (None, 0),
        **quality_service.assess(ts, timestamp_source=timestamp_source, complete=complete, server_timestamp=fetch_timestamp),
    )


def parse_kline_row(row: str) -> Kline:
    values = row.split(",")
    if len(values) < 7:
        raise ProviderError("eastmoney kline row has fewer than 7 fields")
    timestamp = datetime.strptime(values[0], "%Y-%m-%d %H:%M" if " " in values[0] else "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    return Kline(
        timestamp=timestamp,
        open=float(values[1]),
        close=float(values[2]),
        high=float(values[3]),
        low=float(values[4]),
        volume=int(float(values[5])) * 100,
        amount=float(values[6]),
    )


def parse_kline_rows(raw_rows: Any) -> list[Kline]:
    """Normalize both observed Eastmoney encodings: JSON array or space-delimited text."""
    rows = raw_rows.split() if isinstance(raw_rows, str) else (raw_rows or [])
    klines: list[Kline] = []
    for row in rows:
        if not isinstance(row, str):
            continue
        try:
            klines.append(parse_kline_row(row))
        except (ProviderError, ValueError):
            continue
    return klines


class EastmoneyProvider(MarketDataProvider):
    quote_url = "https://push2.eastmoney.com/api/qt/stock/get"
    list_url = "https://push2.eastmoney.com/api/qt/clist/get"
    kline_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def __init__(
        self,
        settings: Settings,
        cache: AsyncTTLCache | None = None,
        quality_service: DataQualityService | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache or AsyncTTLCache()
        self.quality_service = quality_service or DataQualityService(
            settings.stale_after_seconds, settings.old_after_seconds, settings.unavailable_after_seconds
        )
        self._client: httpx.AsyncClient | None = None
        self._sync_proxy_client: httpx.Client | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.eastmoney_timeout),
                limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0 Safari/537.36",
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://quote.eastmoney.com/",
                },
                follow_redirects=True,
                proxy=self.settings.eastmoney_proxy,
            )
            if self.settings.eastmoney_proxy:
                self._sync_proxy_client = httpx.Client(
                    timeout=httpx.Timeout(self.settings.eastmoney_timeout),
                    limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
                    headers=dict(self._client.headers),
                    follow_redirects=True,
                    proxy=self.settings.eastmoney_proxy,
                )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._sync_proxy_client is not None:
            await asyncio.to_thread(self._sync_proxy_client.close)
            self._sync_proxy_client = None

    async def _request(self, url: str, params: dict[str, Any], *, require_key: str | None = None) -> dict[str, Any]:
        await self.start()
        assert self._client is not None
        last_error: Exception | None = None
        if "push2his.eastmoney.com" in url:
            urls = [url, url.replace("push2his.", "push2."), url.replace("push2his.", "push2delay.")]
        else:
            urls = [url, url.replace("push2.", "push2delay."), url.replace("push2.", "push2his.")]
        for attempt in range(self.settings.eastmoney_retries):
            try:
                request_url = urls[min(attempt, len(urls) - 1)]
                if self._sync_proxy_client is not None:
                    response = await asyncio.to_thread(self._sync_proxy_client.get, request_url, params=params)
                else:
                    response = await self._client.get(request_url, params=params)
                response.raise_for_status()
                payload = json.loads(response.content.decode("utf-8"))
                if payload.get("rc") != 0 or payload.get("data") is None:
                    raise ProviderError(f"eastmoney returned rc={payload.get('rc')}")
                if require_key and not payload["data"].get(require_key):
                    raise ProviderError(f"eastmoney returned empty {require_key}")
                return payload
            except (httpx.HTTPError, ValueError, ProviderError) as exc:
                last_error = exc
                if attempt + 1 < self.settings.eastmoney_retries:
                    await asyncio.sleep(0.2 * (2**attempt))
        detail = str(last_error) if last_error else "unknown error"
        raise ProviderError(f"eastmoney request failed after {self.settings.eastmoney_retries} attempts: {detail}")

    async def get_quote(self, code: str) -> Quote:
        code = validate_code(code)

        async def load() -> Quote:
            payload = await self._request(self.quote_url, {"ut": QUOTE_UT, "secid": to_eastmoney_secid(code), "fltt": 2, "invt": 2, "fields": QUOTE_FIELDS})
            return parse_quote(payload["data"], fltt=2, quality_service=self.quality_service)

        return await self.cache.get_or_set(f"quote:{code}", 3, load)

    async def get_index_quote(self, code: str, market: str) -> Quote:
        code = validate_code(code)
        if market not in {"SH", "SZ"}:
            raise ValueError("index market must be SH or SZ")

        async def load() -> Quote:
            secid = f"{1 if market == 'SH' else 0}.{code}"
            payload = await self._request(
                self.quote_url,
                {"ut": QUOTE_UT, "secid": secid, "fltt": 2, "invt": 2, "fields": QUOTE_FIELDS},
            )
            return parse_quote(payload["data"], fltt=2, quality_service=self.quality_service)

        return await self.cache.get_or_set(f"index:{market}:{code}", 3, load)

    async def get_quotes(self, codes: list[str]) -> list[Quote]:
        unique = list(dict.fromkeys(validate_code(code) for code in codes))
        if len(unique) > 100:
            raise ValueError("at most 100 codes are allowed")
        results = await asyncio.gather(*(self.get_quote(code) for code in unique), return_exceptions=True)
        quotes = [item for item in results if isinstance(item, Quote)]
        if not quotes and results:
            first_error = next((item for item in results if isinstance(item, Exception)), None)
            raise ProviderError(f"all quote requests failed: {first_error}")
        return quotes

    async def get_kline(self, code: str, period: str, limit: int) -> KlineResult:
        code = validate_code(code)
        if period not in PERIOD_MAP:
            raise ValueError(f"period must be one of {', '.join(PERIOD_MAP)}")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        ttl = 60 if period in {"day", "week", "month"} else 5

        async def load() -> KlineResult:
            payload = await self._request(
                self.kline_url,
                {
                    "secid": to_eastmoney_secid(code),
                    "ut": QUOTE_UT,
                    "klt": PERIOD_MAP[period],
                    "fqt": 1,
                    "lmt": limit,
                    "end": "20500101",
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                },
                require_key="klines",
            )
            klines = parse_kline_rows(payload["data"].get("klines"))
            if not klines:
                raise ProviderError(f"eastmoney returned no {period} kline for {code}")
            data_ts = klines[-1].timestamp
            if period in {"day", "week", "month"} and data_ts.date() == now_shanghai().date():
                try:
                    data_ts = (await self.get_quote(code)).data_timestamp
                except ProviderError:
                    pass
            return KlineResult(code=code, period=period, klines=klines, **self.quality_service.assess(data_ts))

        return await self.cache.get_or_set(f"kline:{code}:{period}:{limit}", ttl, load)

    async def get_all_a_shares(self) -> tuple[int, list[Quote]]:
        async def load() -> tuple[int, list[Quote]]:
            common = {
                    "pn": 1,
                    "pz": 100,
                    "po": 1,
                    "np": 1,
                    "ut": LIST_UT,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f3",
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                    "fields": LIST_FIELDS,
            }

            async def fetch_page(page: int) -> dict[str, Any]:
                params = {**common, "pn": page}
                return await self._request(
                    self.list_url,
                    params,
                    require_key="diff",
                )

            payload = await fetch_page(1)
            total = int(payload["data"].get("total") or 0)
            page_count = max(1, ceil(total / common["pz"]))
            semaphore = asyncio.Semaphore(min(4, self.settings.scan_concurrency))

            async def guarded(page: int) -> dict[str, Any] | Exception:
                try:
                    async with semaphore:
                        return await fetch_page(page)
                except Exception as exc:
                    return exc

            remaining = await asyncio.gather(*(guarded(page) for page in range(2, page_count + 1)))
            payloads = [payload, *(item for item in remaining if isinstance(item, dict))]
            raw_rows = [row for item in payloads for row in (item["data"].get("diff") or [])]
            quotes: list[Quote] = []
            for row in raw_rows:
                try:
                    quotes.append(parse_quote(row, fltt=2, quality_service=self.quality_service))
                except (ValueError, ProviderError):
                    continue
            # The full-market snapshot seeds the canonical quote cache.  A following
            # stock request therefore reuses the exact normalized Quote object.
            self.cache.set_many({f"quote:{quote.code}": quote for quote in quotes}, ttl=3)
            return total or len(raw_rows), quotes

        return await self.cache.get_or_set("market:all-a-shares", 3, load)

    async def get_sector_ranking(self, sector_type: str, limit: int) -> SectorRanking:
        if sector_type not in {"industry", "concept"}:
            raise ValueError("sector_type must be industry or concept")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        async def load() -> SectorRanking:
            fetched_at = now_shanghai()
            payload = await self._request(
                self.list_url,
                {
                    "pn": 1,
                    "pz": limit,
                    "po": 1,
                    "np": 1,
                    "ut": LIST_UT,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f3",
                    "fs": "m:90+t:2" if sector_type == "industry" else "m:90+t:3",
                    "fields": SECTOR_FIELDS,
                },
                require_key="diff",
            )
            rows = payload["data"].get("diff") or []
            items = [
                SectorItem(
                    name=str(row.get("f14") or ""), code=str(row.get("f12") or ""),
                    pct_change=_number(row.get("f3")), amount=_number(row.get("f6")),
                    up_count=_integer(row.get("f104")), down_count=_integer(row.get("f105")), rank=index,
                )
                for index, row in enumerate(rows, 1)
            ]
            timestamps = [_timestamp(row.get("f124"), fetched_at) for row in rows]
            timestamp, timestamp_source = max(timestamps, key=lambda item: item[0], default=(fetched_at, "fetch_time"))
            return SectorRanking(
                sector_type=sector_type,
                items=items,
                **self.quality_service.assess(
                    timestamp, timestamp_source=timestamp_source, complete=bool(rows), server_timestamp=fetched_at
                ),
            )

        return await self.cache.get_or_set(f"sector:{sector_type}:{limit}", 10, load)
