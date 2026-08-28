from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import httpx

from app.config import Settings
from app.models import Kline, KlineResult, Quote, SectorRanking
from app.providers.base import (
    MarketDataProvider,
    ProviderEmptyDataError,
    ProviderError,
    ProviderParseError,
    ProviderTimeoutError,
    ProviderUnsupportedError,
)
from app.providers.symbols import market_of, validate_code
from app.services.data_quality import DataQualityService
from app.utils.time import SHANGHAI, now_shanghai


def to_tencent_symbol(code: str) -> str:
    code = validate_code(code)
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}[market_of(code)] + code


def _float(values: list[str], index: int) -> float | None:
    if index >= len(values) or values[index] in {"", "-", "--"}:
        return None
    return float(values[index])


def _quote_timestamp(values: list[str], fetched_at: datetime) -> datetime:
    try:
        return datetime.strptime(values[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
    except (IndexError, ValueError):
        return fetched_at


def parse_tencent_quote(
    raw: str, fetched_at: datetime, quality: DataQualityService, market_override: str | None = None
) -> Quote:
    if '="' not in raw:
        raise ProviderParseError("tencent quote response has no payload")
    payload = raw.split('="', 1)[1].rsplit('"', 1)[0]
    values = payload.split("~")
    if len(values) < 44:
        raise ProviderParseError(f"tencent quote has only {len(values)} fields")
    code = validate_code(values[2])
    price = _float(values, 3)
    if price is None or price <= 0:
        raise ProviderEmptyDataError(f"tencent returned invalid price for {code}")
    timestamp = _quote_timestamp(values, fetched_at)
    volume_lots = _float(values, 6)
    amount = None
    try:
        amount = float(values[35].split("/")[2])
    except (IndexError, ValueError):
        pass
    return Quote(
        code=code,
        name=values[1],
        market=market_override or market_of(code),
        price=price,
        prev_close=_float(values, 4),
        open=_float(values, 5),
        high=_float(values, 33),
        low=_float(values, 34),
        pct_change=_float(values, 32),
        change=_float(values, 31),
        volume=None if volume_lots is None else int(volume_lots * 100),
        amount=amount,
        turnover_rate=_float(values, 38),
        volume_ratio=None,
        amplitude=_float(values, 43),
        suspended=False,
        **quality.assess(
            timestamp,
            timestamp_source="tencent",
            source="tencent",
            complete=all(_float(values, index) is not None for index in (3, 4, 5, 33, 34)),
            server_timestamp=fetched_at,
        ),
    )


def parse_tencent_kline_rows(rows: list[list[str]]) -> list[Kline]:
    result: list[Kline] = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            result.append(
                Kline(
                    timestamp=datetime.strptime(row[0], "%Y-%m-%d").replace(tzinfo=SHANGHAI),
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume=int(float(row[5]) * 100),
                    amount=0.0,
                )
            )
        except (ValueError, TypeError):
            continue
    return result


class TencentProvider(MarketDataProvider):
    quote_url = "https://qt.gtimg.cn/q={symbol}"
    kline_url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

    def __init__(self, settings: Settings, quality: DataQualityService) -> None:
        self.settings = settings
        self.quality = quality
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.tencent_timeout),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://gu.qq.com/",
                },
                follow_redirects=True,
                proxy=self.settings.tencent_proxy,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        await self.start()
        assert self._client is not None
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            return response
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"tencent timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"tencent HTTP error: {exc}") from exc

    async def get_quote(self, code: str) -> Quote:
        fetched_at = now_shanghai()
        response = await self._get(self.quote_url.format(symbol=to_tencent_symbol(code)))
        try:
            raw = response.content.decode("gbk")
        except UnicodeDecodeError as exc:
            raise ProviderParseError(f"tencent quote decode failed: {exc}") from exc
        return parse_tencent_quote(raw, fetched_at, self.quality)

    async def get_index_quote(self, code: str, market: str) -> Quote:
        if market not in {"SH", "SZ"}:
            raise ValueError("index market must be SH or SZ")
        code = validate_code(code)
        fetched_at = now_shanghai()
        response = await self._get(self.quote_url.format(symbol=market.lower() + code))
        try:
            raw = response.content.decode("gbk")
        except UnicodeDecodeError as exc:
            raise ProviderParseError(f"tencent index quote decode failed: {exc}") from exc
        return parse_tencent_quote(raw, fetched_at, self.quality, market_override=market)

    async def get_quotes(self, codes: list[str]) -> list[Quote]:
        results = await asyncio.gather(*(self.get_quote(code) for code in dict.fromkeys(codes)), return_exceptions=True)
        quotes = [value for value in results if isinstance(value, Quote)]
        if not quotes and results:
            error = next((value for value in results if isinstance(value, Exception)), None)
            raise ProviderError(f"all Tencent quotes failed: {error}")
        return quotes

    async def get_kline(
        self, code: str, period: str, limit: int, adjust: str = "qfq", *, quote: Quote | None = None
    ) -> KlineResult:
        code = validate_code(code)
        if period != "day":
            raise ProviderUnsupportedError("Tencent secondary currently supports daily K-line only")
        if adjust not in {"qfq", "raw", "hfq"}:
            raise ValueError("adjust must be qfq, raw or hfq")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        symbol = to_tencent_symbol(code)
        response = await self._get(
            self.kline_url,
            {"param": f"{symbol},day,,,{limit},{adjust}"},
        )
        try:
            payload = json.loads(response.content.decode("utf-8"))
            data = (payload.get("data") or {}).get(symbol) or {}
            key = {"qfq": "qfqday", "raw": "day", "hfq": "hfqday"}[adjust]
            rows = data.get(key) or []
        except (UnicodeDecodeError, ValueError, AttributeError) as exc:
            raise ProviderParseError(f"tencent K-line parse failed: {exc}") from exc
        klines = parse_tencent_kline_rows(rows)[-limit:]
        if not klines:
            raise ProviderEmptyDataError(f"tencent returned empty {adjust} day K-line for {code}")
        qt_values = (data.get("qt") or {}).get(symbol) or []
        fetched_at = now_shanghai()
        timestamp = _quote_timestamp(qt_values, fetched_at) if qt_values else klines[-1].timestamp
        return KlineResult(
            code=code,
            period=period,
            klines=klines,
            **self.quality.assess(
                timestamp,
                timestamp_source="tencent" if qt_values else "fetch_time",
                source="tencent",
                complete=True,
                server_timestamp=fetched_at,
            ),
        )

    async def get_all_a_shares(self) -> tuple[int, list[Quote]]:
        raise ProviderUnsupportedError("Tencent provider does not expose the project full-market universe")

    async def get_sector_ranking(self, sector_type: str, limit: int) -> SectorRanking:
        raise ProviderUnsupportedError("Tencent provider does not expose project sector ranking")
