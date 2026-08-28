from __future__ import annotations

import hmac
import asyncio
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.container import container
from app.serialization import serialize_business
from app.api.live import (
    LiveSnapshotCache,
    cached_response,
    initializing_page,
    log_live_response,
    unavailable_stock_page,
)

router = APIRouter()


async def _load_live_snapshot():
    market, scan, industry, concept = await asyncio.gather(
        container.market.get_market_overview(),
        container.scanner.scan_mainboard(top_n=30),
        container.sectors.get_sector_ranking("industry", 20),
        container.sectors.get_sector_ranking("concept", 20),
        return_exceptions=True,
    )
    if isinstance(market, Exception):
        raise market
    if isinstance(scan, Exception):
        raise scan
    quotes = {}
    if scan.candidates:
        try:
            items = await container.quotes.get_quotes([item.code for item in scan.candidates])
            quotes = {item.code: item for item in items}
        except Exception:
            # Market + scanner are already a valid successful snapshot. Quote
            # enrichment is best-effort and must not discard that snapshot.
            pass
    coverage = await container.scanner.get_scan_coverage(scan.scan_id)

    def sector_counts(value):
        if isinstance(value, Exception):
            return None, 0, 1
        total = value.total_count if value.total_count is not None else len(value.items)
        success = value.success_count if value.success_count is not None else len(value.items)
        return total, success, value.failed_count or 0

    industry_total, industry_success, industry_failed = sector_counts(industry)
    concept_total, concept_success, concept_failed = sector_counts(concept)
    failure_sources = dict(coverage.failure_sources)
    failure_sources["sector"] = industry_failed + concept_failed
    coverage = coverage.model_copy(
        update={
            "industry_total": industry_total,
            "industry_success": industry_success,
            "concept_total": concept_total,
            "concept_success": concept_success,
            "failure_sources": failure_sources,
        }
    )
    return (
        market,
        scan,
        quotes,
        coverage,
        None if isinstance(industry, Exception) else industry,
        None if isinstance(concept, Exception) else concept,
    )


live_cache = LiveSnapshotCache(_load_live_snapshot)


def _web_secret() -> str | None:
    settings = get_settings()
    return settings.gpt_web_secret or settings.mcp_token


def require_web_secret(secret: str) -> None:
    expected = _web_secret()
    if not expected:
        raise HTTPException(status_code=503, detail="GPT Web API secret is not configured")
    if not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=404, detail="not found")


def web_response(value: Any) -> dict[str, Any]:
    return {"ok": True, "data": serialize_business(value)}


def _html(value: Any) -> str:
    return "" if value is None else str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def v2_scan_page(result, secret: str) -> HTMLResponse:
    rows = []
    for rank, item in enumerate(result.raw_top30, 1):
        breakdown = "".join(
            f"<li><strong>{_html(name)}</strong>: {_html(part.score)} / {_html(part.max_score)}; "
            f"coverage={_html(part.coverage)}; reason={_html('；'.join(part.reason))}; raw={_html(part.raw_value)}</li>"
            for name, part in item.score_breakdown.items()
        )
        rows.append(
            "<tr>"
            f"<td>{rank}</td><td>{_html(item.stock_code)}</td><td>{_html(item.stock_name)}</td>"
            f"<td>{_html(item.opportunity_score)}</td><td>{_html(item.grade)}</td>"
            f"<td>{_html(item.position_score)}</td><td>{_html(item.fundamental_score)}</td>"
            f"<td>{_html(item.trend_score)}</td><td>{_html(item.flow_score)}</td>"
            f"<td>{_html(item.catalyst_score)}</td><td>{_html(item.risk_reward_score)}</td>"
            f"<td>{_html(item.liquidity_score)}</td><td>{_html(item.risk_penalty)}</td>"
            f"<td>{_html(item.support)}</td><td>{_html(item.resistance)}</td><td>{_html(item.stop_loss)}</td>"
            f"<td>{_html(item.target_1)}</td><td>{_html(item.target_2)}</td><td>{_html(item.risk_reward_ratio)}</td>"
            f"<td>{_html(item.week_trend)} / {_html(item.day_trend)}</td>"
            f"<td><details><summary>评分拆解</summary><p>{_html(item.score_formula)}</p><ul>{breakdown}</ul></details></td>"
            "</tr>"
        )
    body = (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>scan_mainboard V2</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;color:#1f2937}"
        "table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #d1d5db;padding:6px;vertical-align:top}"
        "th{background:#f3f4f6;position:sticky;top:0}details{max-width:520px}code{white-space:pre-wrap}</style></head><body>"
        f"<h1>scan_mainboard V2 Top30</h1><p>score_version={_html(result.score_version)}; "
        f"candidate_pool_size={_html(result.candidate_pool_size)}; duration_seconds={_html(result.duration_seconds)}; "
        f"coverage={_html(result.coverage.coverage_rate)}</p>"
        f"<p>公式：{_html(result.score_formula)}</p>"
        f"<p>缺失数据源：{_html(', '.join(result.missing_data_sources))}</p>"
        f"<p><a href=\"/gpt/{_html(secret)}/scan/v2\">JSON</a> · <a href=\"/gpt/{_html(secret)}/scan/ab\">V1 vs V2 JSON</a></p>"
        "<table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>机会分</th><th>等级</th>"
        "<th>位置</th><th>基本面</th><th>趋势</th><th>资金量价</th><th>催化</th><th>RR</th><th>流动性</th><th>风险扣分</th>"
        "<th>支撑</th><th>压力</th><th>止损</th><th>目标1</th><th>目标2</th><th>RR</th><th>周/日趋势</th><th>展开</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )
    return HTMLResponse(body, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/providers")
async def provider_health():
    return {
        "status": "ok",
        **container.market_data.health(),
        "last_scan": container.scanner.last_summary,
    }


@router.get("/quote/{code}")
async def quote(code: str):
    return await container.quotes.get_quote(code)


@router.get("/quotes")
async def quotes(codes: list[str] = Query(...)):
    return await container.quotes.get_quotes(codes)


@router.get("/kline/{code}")
async def kline(code: str, period: str = "day", limit: int = 120):
    return await container.klines.get_kline(code, period, limit)


@router.get("/detail/{code}")
async def detail(code: str):
    return await container.klines.get_stock_detail(code)


@router.get("/market")
async def market():
    return await container.market.get_market_overview()


@router.get("/sectors")
async def sectors(sector_type: str = "industry", limit: int = 30):
    return await container.sectors.get_sector_ranking(sector_type, limit)


@router.get("/scan")
async def scan(
    top_n: int = 30,
    max_pct_change: float = 5.0,
    min_amount: float = 50_000_000,
    exclude_st: bool = True,
    exclude_limit_up: bool = True,
    exclude_limit_down: bool = True,
):
    return await container.scanner.scan_mainboard(
        top_n, max_pct_change, min_amount, exclude_st, exclude_limit_up, exclude_limit_down
    )


@router.get("/scan/v2")
async def scan_v2(
    top_n: int = 30,
    pool_size: int = 420,
    min_amount: float = 50_000_000,
    exclude_st: bool = True,
    exclude_limit_up: bool = True,
    exclude_limit_down: bool = True,
):
    return await container.scanner.scan_mainboard_v2(
        top_n, pool_size, min_amount, exclude_st, exclude_limit_up, exclude_limit_down
    )


@router.get("/scan/coverage")
async def scan_coverage():
    return await container.scanner.get_scan_coverage()


# GPT Web Adapter.  These handlers contain no market parsing, calculations or
# cache of their own; they call the exact same singleton services as MCP.
@router.get("/gpt/{secret}/stock/{code}", dependencies=[Depends(require_web_secret)])
async def gpt_quote(secret: str, code: str):
    return web_response(await container.quotes.get_quote(code))


@router.get("/gpt/{secret}/stocks", dependencies=[Depends(require_web_secret)])
async def gpt_quotes(secret: str, codes: list[str] = Query(...)):
    return web_response(await container.quotes.get_quotes(codes))


@router.get("/gpt/{secret}/stock/{code}/kline", dependencies=[Depends(require_web_secret)])
async def gpt_kline(secret: str, code: str, period: str = "day", limit: int = 120):
    return web_response(await container.klines.get_kline(code, period, limit))


@router.get("/gpt/{secret}/stock/{code}/detail", dependencies=[Depends(require_web_secret)])
async def gpt_detail(secret: str, code: str):
    return web_response(await container.klines.get_stock_detail(code))


@router.get("/gpt/{secret}/market", dependencies=[Depends(require_web_secret)])
async def gpt_market(secret: str):
    return web_response(await container.market.get_market_overview())


@router.get("/gpt/{secret}/sectors", dependencies=[Depends(require_web_secret)])
async def gpt_sectors(secret: str, sector_type: str = "industry", limit: int = 30):
    return web_response(await container.sectors.get_sector_ranking(sector_type, limit))


@router.get("/gpt/{secret}/scan", dependencies=[Depends(require_web_secret)])
async def gpt_scan(
    secret: str,
    top_n: int = 30,
    max_pct_change: float = 5.0,
    min_amount: float = 50_000_000,
    exclude_st: bool = True,
    exclude_limit_up: bool = True,
    exclude_limit_down: bool = True,
):
    result = await container.scanner.scan_mainboard(
        top_n, max_pct_change, min_amount, exclude_st, exclude_limit_up, exclude_limit_down
    )
    return web_response(result)


@router.get("/gpt/{secret}/scan/v2", dependencies=[Depends(require_web_secret)])
async def gpt_scan_v2(
    secret: str,
    top_n: int = 30,
    pool_size: int = 420,
    min_amount: float = 50_000_000,
    exclude_st: bool = True,
    exclude_limit_up: bool = True,
    exclude_limit_down: bool = True,
):
    result = await container.scanner.scan_mainboard_v2(
        top_n, pool_size, min_amount, exclude_st, exclude_limit_up, exclude_limit_down
    )
    return web_response(result)


@router.get("/gpt/{secret}/scan/v2/html", dependencies=[Depends(require_web_secret)])
async def gpt_scan_v2_html(
    secret: str,
    top_n: int = 30,
    pool_size: int = 420,
    min_amount: float = 50_000_000,
):
    result = await container.scanner.scan_mainboard_v2(top_n, pool_size, min_amount)
    return v2_scan_page(result, secret)


@router.get("/gpt/{secret}/scan/ab", dependencies=[Depends(require_web_secret)])
async def gpt_scan_ab(secret: str, top_n: int = 30):
    v1, v2 = await asyncio.gather(
        container.scanner.scan_mainboard(top_n=top_n),
        container.scanner.scan_mainboard_v2(top_n=top_n),
    )
    return web_response({"v1_top30": v1, "v2_top30": v2})


@router.get("/gpt/{secret}/scan/coverage", dependencies=[Depends(require_web_secret)])
async def gpt_scan_coverage(secret: str):
    return web_response(await container.scanner.get_scan_coverage())


@router.get("/gpt/{secret}/coverage", dependencies=[Depends(require_web_secret)])
async def gpt_coverage(secret: str):
    snapshot = live_cache.get().snapshot
    if snapshot is None or snapshot.coverage is None:
        raise HTTPException(status_code=503, detail="coverage snapshot is initializing")
    return web_response(snapshot.coverage)


# Live Refresh Adapter. Nonces make every navigation URL unique; the handlers
# remain thin transport views over the same singleton services used by MCP/JSON.
@router.get("/gpt/{secret}/live", dependencies=[Depends(require_web_secret)])
async def gpt_live(secret: str):
    started = perf_counter()
    try:
        view = live_cache.get()
        return initializing_page() if view.snapshot is None else cached_response(view.snapshot.html_template, secret, view)
    finally:
        log_live_response(started)


@router.get("/gpt/{secret}/live/{request_nonce}", dependencies=[Depends(require_web_secret)])
async def gpt_live_snapshot(secret: str, request_nonce: str):
    started = perf_counter()
    try:
        view = live_cache.get()
        return initializing_page() if view.snapshot is None else cached_response(view.snapshot.html_template, secret, view)
    finally:
        log_live_response(started)


@router.get("/gpt/{secret}/live/{request_nonce}/stock/{code}", dependencies=[Depends(require_web_secret)])
async def gpt_live_stock(secret: str, request_nonce: str, code: str):
    started = perf_counter()
    try:
        view = live_cache.get()
        if view.snapshot is None:
            return initializing_page()
        template = view.snapshot.stock_html_templates.get(code)
        return cached_response(template, secret, view) if template is not None else unavailable_stock_page(secret, code, view)
    finally:
        log_live_response(started)
