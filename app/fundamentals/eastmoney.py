from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx

from app.fundamentals.base import FundamentalProvider, FundamentalProviderError
from app.models import FundamentalField, FundamentalQuarter, FundamentalSnapshot
from app.providers.symbols import market_of, validate_code
from app.utils.time import SHANGHAI, now_shanghai


DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
F10_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew"
VALUATION_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"


def _secid(code: str) -> str:
    return f"{1 if market_of(code) == 'SH' else 0}.{validate_code(code)}"


def _dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")).replace(tzinfo=SHANGHAI)
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _field(
    value: object,
    *,
    report_period: str | None,
    fetch_time: datetime,
    source: str,
    upstream_source: str = "eastmoney",
    confidence: str = "MEDIUM",
    error: str | None = None,
) -> FundamentalField:
    return FundamentalField(
        value=value,
        source=source,
        upstream_source=upstream_source,
        source_type="vendor",
        report_period=report_period,
        fetch_time=fetch_time,
        coverage=value is not None,
        stale=False,
        confidence=confidence,
        error=error,
    )


def _quarter(row: dict[str, Any]) -> FundamentalQuarter:
    values = {
        "revenue": _number(row.get("TOTALOPERATEREVE")),
        "revenue_yoy": _number(row.get("DJD_TOI_YOY") if row.get("DJD_TOI_YOY") is not None else row.get("TOTALOPERATEREVETZ")),
        "revenue_qoq": _number(row.get("DJD_TOI_QOQ")),
        "net_profit": _number(row.get("PARENTNETPROFIT")),
        "net_profit_yoy": _number(row.get("DJD_DPNP_YOY") if row.get("DJD_DPNP_YOY") is not None else row.get("PARENTNETPROFITTZ")),
        "net_profit_qoq": _number(row.get("DJD_DPNP_QOQ")),
        "deducted_net_profit": _number(row.get("KCFJCXSYJLR")),
        "deducted_net_profit_yoy": _number(row.get("DJD_DEDUCTDPNP_YOY") if row.get("DJD_DEDUCTDPNP_YOY") is not None else row.get("KCFJCXSYJLRTZ")),
        "operating_cash_flow": _number(row.get("NETCASH_OPERATE_PK")),
        "roe": _number(row.get("ROEJQ")),
        "gross_margin": _number(row.get("XSMLL")),
        "debt_ratio": _number(row.get("ZCFZL")),
    }
    return FundamentalQuarter(
        report_period=str(row.get("REPORT_DATE") or "")[:10] or None,
        notice_date=_dt(row.get("NOTICE_DATE")),
        **values,
        fetch_time=now_shanghai(),
        coverage=round(sum(value is not None for value in values.values()) / len(values), 4),
    )


def snapshot_from_rows(
    code: str,
    rows: list[dict[str, Any]],
    *,
    source: str,
    valuation: dict[str, Any] | None = None,
    forecast: dict[str, Any] | None = None,
    express: dict[str, Any] | None = None,
) -> FundamentalSnapshot:
    fetch_time = now_shanghai()
    ordered = sorted(rows, key=lambda row: str(row.get("REPORT_DATE") or ""), reverse=True)[:8]
    latest = ordered[0] if ordered else {}
    period = str(latest.get("REPORT_DATE") or "")[:10] or None
    mapping = {
        "revenue": "TOTALOPERATEREVE",
        "revenue_yoy": "DJD_TOI_YOY",
        "revenue_qoq": "DJD_TOI_QOQ",
        "net_profit": "PARENTNETPROFIT",
        "deducted_net_profit": "KCFJCXSYJLR",
        "net_profit_yoy": "DJD_DPNP_YOY",
        "net_profit_qoq": "DJD_DPNP_QOQ",
        "roe": "ROEJQ",
        "operating_cash_flow": "NETCASH_OPERATE_PK",
        "gross_margin": "XSMLL",
        "debt_ratio": "ZCFZL",
    }
    fields = {
        name: _field(_number(latest.get(key)), report_period=period, fetch_time=fetch_time, source=source)
        for name, key in mapping.items()
    }
    valuation = valuation or {}
    fields["pe"] = _field(_number(valuation.get("f9")), report_period=None, fetch_time=fetch_time, source="eastmoney_quote")
    fields["pb"] = _field(_number(valuation.get("f23")), report_period=None, fetch_time=fetch_time, source="eastmoney_quote")
    fields["industry"] = _field(valuation.get("f100") or None, report_period=None, fetch_time=fetch_time, source="eastmoney_quote")
    quarters = [_quarter(row) for row in ordered]
    forecast_field = _field(
        None, report_period=None, fetch_time=fetch_time, source="eastmoney_performance_forecast",
        error="no recent performance forecast",
    )
    if forecast:
        forecast_period = str(forecast.get("REPORT_DATE") or "")[:10] or None
        forecast_field = _field(
            {
                "type": forecast.get("PREDICT_TYPE"),
                "profit_lower": _number(forecast.get("PREDICT_AMT_LOWER")),
                "profit_upper": _number(forecast.get("PREDICT_AMT_UPPER")),
                "yoy_lower": _number(forecast.get("ADD_AMP_LOWER")),
                "yoy_upper": _number(forecast.get("ADD_AMP_UPPER")),
                "reason": forecast.get("CHANGE_REASON_EXPLAIN"),
            },
            report_period=forecast_period,
            fetch_time=fetch_time,
            source="eastmoney_performance_forecast",
        )
    express_field = _field(
        None, report_period=None, fetch_time=fetch_time, source="eastmoney_performance_express",
        error="no recent performance express",
    )
    if express:
        express_period = str(express.get("REPORT_DATE") or "")[:10] or None
        express_field = _field(
            {
                "revenue": _number(express.get("TOTAL_OPERATE_INCOME")),
                "revenue_yoy": _number(express.get("YSTZ")),
                "net_profit": _number(express.get("PARENT_NETPROFIT")),
                "net_profit_yoy": _number(express.get("JLRTBZCL")),
                "roe": _number(express.get("WEIGHTAVG_ROE")),
            },
            report_period=express_period,
            fetch_time=fetch_time,
            source="eastmoney_performance_express",
        )
    return FundamentalSnapshot(
        code=code,
        fields=fields,
        quarterly_trend=quarters,
        performance_forecast=forecast_field,
        performance_express=express_field,
        audit_opinion=_field(
            None,
            report_period=period,
            fetch_time=fetch_time,
            source="fundamental_manager",
            upstream_source="none",
            confidence="LOW",
            error="audit opinion source is not available in Phase2A",
        ),
        report_period=period,
        fetch_time=fetch_time,
        source=source,
        upstream_sources=["eastmoney"],
    ).with_coverage()


class EastmoneyDatacenterFundamentalProvider(FundamentalProvider):
    name = "eastmoney_datacenter"
    upstream_source = "eastmoney"

    def __init__(self, timeout: float = 8.0, proxy: str | None = None) -> None:
        self.client = httpx.AsyncClient(timeout=timeout, proxy=proxy, follow_redirects=True)

    async def _report(
        self, report_name: str, codes: list[str], page_size: int, sort_column: str = "REPORT_DATE"
    ) -> list[dict[str, Any]]:
        expression = ",".join(f'"{validate_code(code)}"' for code in codes)
        response = await self.client.get(
            DATACENTER_URL,
            params={
                "reportName": report_name,
                "columns": "ALL",
                "filter": f"(SECURITY_CODE in ({expression}))",
                "pageNumber": 1,
                "pageSize": page_size,
                "sortTypes": -1,
                "sortColumns": sort_column,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise FundamentalProviderError(f"eastmoney {report_name} failed: {payload.get('message')}")
        return ((payload.get("result") or {}).get("data") or [])

    async def _valuation(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        secids = ",".join(_secid(code) for code in codes)
        response = await self.client.get(
            VALUATION_URL,
            params={"fltt": 2, "invt": 2, "fields": "f12,f9,f23,f100", "secids": secids},
        )
        response.raise_for_status()
        payload = response.json()
        return {str(row.get("f12")): row for row in ((payload.get("data") or {}).get("diff") or [])}

    async def get_many(self, codes: list[str]) -> dict[str, FundamentalSnapshot]:
        unique = list(dict.fromkeys(validate_code(code) for code in codes))
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        valuations: dict[str, dict[str, Any]] = {}
        forecasts: dict[str, dict[str, Any]] = {}
        expresses: dict[str, dict[str, Any]] = {}
        datacenter_valuations: dict[str, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(3)

        async def load_chunk(chunk: list[str]) -> None:
            async with semaphore:
                results = await asyncio.gather(
                    self._report("RPT_F10_FINANCE_MAINFINADATA", chunk, max(100, len(chunk) * 8)),
                    self._valuation(chunk),
                    self._report("RPT_PUBLIC_OP_NEWPREDICT", chunk, max(20, len(chunk) * 3)),
                    self._report("RPT_FCI_PERFORMANCEE", chunk, max(20, len(chunk) * 3)),
                    self._report("RPT_VALUEANALYSIS_DET", chunk, max(20, len(chunk) * 3), "TRADE_DATE"),
                    return_exceptions=True,
                )
            rows, value_rows, forecast_rows, express_rows, valuation_rows = results
            if isinstance(rows, Exception):
                raise rows
            value_rows = {} if isinstance(value_rows, Exception) else value_rows
            forecast_rows = [] if isinstance(forecast_rows, Exception) else forecast_rows
            express_rows = [] if isinstance(express_rows, Exception) else express_rows
            valuation_rows = [] if isinstance(valuation_rows, Exception) else valuation_rows
            for row in rows:
                grouped[str(row.get("SECURITY_CODE"))].append(row)
            valuations.update(value_rows)
            for row in valuation_rows:
                code = str(row.get("SECURITY_CODE"))
                if code and code not in datacenter_valuations:
                    datacenter_valuations[code] = row
            for target, rows_to_add in ((forecasts, forecast_rows), (expresses, express_rows)):
                for row in rows_to_add:
                    code = str(row.get("SECURITY_CODE"))
                    if code and code not in target:
                        target[code] = row

        chunks = [unique[index:index + 40] for index in range(0, len(unique), 40)]
        await asyncio.gather(*(load_chunk(chunk) for chunk in chunks))
        return {
            code: snapshot_from_rows(
                code,
                grouped.get(code, []),
                source=self.name,
                valuation=valuations.get(code) or {
                    "f9": (datacenter_valuations.get(code) or {}).get("PE_TTM"),
                    "f23": (datacenter_valuations.get(code) or {}).get("PB_MRQ"),
                    "f100": (datacenter_valuations.get(code) or {}).get("BOARD_NAME"),
                },
                forecast=forecasts.get(code),
                express=expresses.get(code),
            )
            for code in unique
        }

    async def close(self) -> None:
        await self.client.aclose()


class EastmoneyF10FundamentalProvider(FundamentalProvider):
    """Independent F10 endpoint used only when the batch datacenter source misses."""

    name = "eastmoney_f10"
    upstream_source = "eastmoney"

    def __init__(self, timeout: float = 8.0, proxy: str | None = None, concurrency: int = 10) -> None:
        self.client = httpx.AsyncClient(timeout=timeout, proxy=proxy, follow_redirects=True)
        self.concurrency = concurrency

    async def get_many(self, codes: list[str]) -> dict[str, FundamentalSnapshot]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def load(code: str) -> tuple[str, FundamentalSnapshot | None]:
            market = "SH" if code.startswith(("5", "6", "9")) else "SZ"
            try:
                async with semaphore:
                    response = await self.client.get(F10_URL, params={"type": 0, "code": f"{market}{code}"})
                response.raise_for_status()
                rows = response.json().get("data") or []
                return code, snapshot_from_rows(code, rows, source=self.name)
            except Exception:
                return code, None

        values = await asyncio.gather(*(load(validate_code(code)) for code in dict.fromkeys(codes)))
        return {code: snapshot for code, snapshot in values if snapshot is not None}

    async def close(self) -> None:
        await self.client.aclose()
