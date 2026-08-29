from __future__ import annotations

from datetime import date, datetime
import json
from typing import Any

import httpx

from app.providers.base import MarketDataProvider
from app.providers.symbols import market_of, validate_code
from app.utils.time import now_shanghai
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    HistoricalBarFetchResult,
    MarketBar,
)


class LegacyHistoricalBarProvider:
    def __init__(self, code: str, provider: MarketDataProvider) -> None:
        self.code = code
        self._provider = provider

    async def fetch(
        self, code: str, period: BarPeriod, adjust_type: AdjustType, limit: int
    ) -> HistoricalBarFetchResult:
        fetched_at = now_shanghai()
        result = await self._provider.get_kline(
            code,
            period.value.lower(),
            limit,
            adjust_type.value.lower(),
        )
        fetched_at = now_shanghai()
        return HistoricalBarFetchResult(
            source_code=self.code,
            upstream_source=result.source,
            code=code,
            period=period,
            adjust_type=adjust_type,
            fetch_time=fetched_at,
            bars=tuple(
                MarketBar(
                    bar_time=item.timestamp,
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    volume=item.volume,
                    amount=None if self.code == "tencent" else item.amount,
                    provisional=item.provisional,
                    fetch_time=fetched_at,
                )
                for item in result.klines
            ),
        )

    async def close(self) -> None:
        return None


class SinaHistoricalBarProvider:
    code = "sina"
    kline_url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{symbol}_240_1/CN_MarketDataService.getKLineData"
    factor_url = "https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"

    def __init__(self, *, client: httpx.AsyncClient | None = None, timeout: float = 20) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._raw_cache: dict[tuple[str, int], tuple[datetime, list[dict[str, Any]]]] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _symbol(code: str) -> str:
        code = validate_code(code)
        return {"SH": "sh", "SZ": "sz", "BJ": "bj"}[market_of(code)] + code

    @staticmethod
    def _decode_json_value(text: str, opening: str) -> Any:
        start = text.find(opening)
        if start < 0:
            raise ValueError("Sina payload has no JSON value")
        return json.JSONDecoder().raw_decode(text[start:])[0]

    async def _raw_rows(self, code: str, limit: int) -> tuple[datetime, list[dict[str, Any]]]:
        key = (code, limit)
        if key in self._raw_cache:
            return self._raw_cache[key]
        symbol = self._symbol(code)
        client = await self._get_client()
        response = await client.get(
            self.kline_url.format(symbol=symbol),
            params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": limit},
        )
        response.raise_for_status()
        rows = self._decode_json_value(response.text, "[")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"Sina returned no daily bars for {code}")
        fetched_at = now_shanghai()
        value = (fetched_at, rows)
        self._raw_cache[key] = value
        return value

    async def _qfq_factors(self, code: str) -> list[tuple[date, float]]:
        symbol = self._symbol(code)
        client = await self._get_client()
        response = await client.get(self.factor_url.format(symbol=symbol))
        response.raise_for_status()
        payload = self._decode_json_value(response.text, "{")
        rows = payload.get("data") if isinstance(payload, dict) else None
        factors = sorted(
            (
                (date.fromisoformat(str(item["d"])), float(item["f"]))
                for item in (rows or [])
                if item.get("d") and item.get("f")
            ),
            reverse=True,
        )
        if not factors:
            raise ValueError(f"Sina returned no QFQ factors for {code}")
        return factors

    @staticmethod
    def _factor_for(value: date, factors: list[tuple[date, float]]) -> float:
        for effective_date, factor in factors:
            if effective_date <= value:
                return factor
        return factors[-1][1]

    async def fetch(
        self, code: str, period: BarPeriod, adjust_type: AdjustType, limit: int
    ) -> HistoricalBarFetchResult:
        if period is not BarPeriod.DAY:
            raise ValueError("Sina V3 adapter supports DAY bars only")
        if adjust_type not in {AdjustType.RAW, AdjustType.QFQ}:
            raise ValueError("Sina V3 adapter supports RAW and QFQ only")
        fetched_at, rows = await self._raw_rows(code, limit)
        factors = await self._qfq_factors(code) if adjust_type is AdjustType.QFQ else None
        bars = []
        for row in rows:
            try:
                trading_date = date.fromisoformat(str(row["day"]))
                factor = self._factor_for(trading_date, factors) if factors else 1.0
                prices = [float(row[key]) / factor for key in ("open", "high", "low", "close")]
                bars.append(
                    MarketBar(
                        bar_time=datetime.combine(trading_date, datetime.min.time(), tzinfo=fetched_at.tzinfo),
                        open=prices[0],
                        high=prices[1],
                        low=prices[2],
                        close=prices[3],
                        volume=int(float(row["volume"])),
                        amount=None,
                        fetch_time=fetched_at,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if not bars:
            raise ValueError(f"Sina returned no valid {adjust_type.value} bars for {code}")
        return HistoricalBarFetchResult(
            source_code=self.code,
            upstream_source="sina_finance",
            code=code,
            period=period,
            adjust_type=adjust_type,
            fetch_time=fetched_at,
            bars=tuple(sorted(bars, key=lambda item: item.bar_time)),
        )
