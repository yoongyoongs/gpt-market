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
    NO_CACHE_HEADERS,
    cached_response,
    initializing_page,
    log_live_response,
    unavailable_stock_page,
)
from app.api.v2_dashboard_cache import V2DashboardCache

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


async def _load_v2_dashboard():
    return await container.scanner.scan_mainboard_v2(top_n=30, pool_size=420, min_amount=50_000_000)


v2_dashboard_cache = V2DashboardCache(_load_v2_dashboard)


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


def _coverage_label(component_name: str, covered: bool) -> str:
    if covered:
        return "可用"
    if component_name == "catalyst":
        return "后续接入"
    if component_name == "trend":
        return "周K暂不可用"
    return "不可用"


def _data_status(value: bool, unavailable_text: str = "暂不可用") -> str:
    return "可用" if value else unavailable_text


def _planned_source_labels(missing_fields: list[str]) -> str:
    labels = {
        "fundamental_financials": "基本面",
        "valuation_industry_relative": "估值/行业比较",
        "announcements_news_policy_catalysts": "公告/新闻/政策催化",
        "main_or_big_order_flow": "主力/大单资金",
        "industry_classification_for_action_top30_concentration": "行业分类",
    }
    values = [label for field, label in labels.items() if field in missing_fields]
    return "、".join(values) or "-"


def v2_scan_page(result, secret: str) -> HTMLResponse:
    rows = []
    for rank, item in enumerate(result.raw_top30, 1):
        score_parts = "".join(
            "<tr>"
            f"<th>{_html(name)}</th>"
            f"<td>{_html(part.score)}</td>"
            f"<td>{_html(part.max_score)}</td>"
            f"<td>{_html(_coverage_label(name, part.coverage))}</td>"
            f"<td>{_html('；'.join(part.reason) or '-')}</td>"
            f"<td><code>{_html(part.raw_value)}</code></td>"
            "</tr>"
            for name, part in item.score_breakdown.items()
        )
        coverage = item.data_coverage
        fundamental = item.raw_inputs.get("fundamental") or {}
        fundamental_fields = fundamental.get("fields") or {}
        fundamental_sources = "、".join(fundamental.get("upstream_sources") or []) or "-"
        fundamental_period = fundamental.get("report_period") or "-"
        fundamental_coverage = round(float(fundamental.get("coverage") or 0) * 100, 2)
        fundamental_rows = "".join(
            "<tr>"
            f"<th>{_html(name)}</th>"
            f"<td>{_html(field.get('value'))}</td>"
            f"<td>{_html(field.get('source'))}</td>"
            f"<td>{_html(field.get('upstream_source'))}</td>"
            f"<td>{_html(field.get('report_period'))}</td>"
            f"<td>{_html('可用' if field.get('coverage') else '缺失')}</td>"
            f"<td>{_html(field.get('confidence'))}</td>"
            f"<td>{_html(field.get('error'))}</td>"
            "</tr>"
            for name, field in fundamental_fields.items()
        )
        planned_missing = _planned_source_labels(item.data_quality.missing_fields)
        rows.append(
            f"""
<tr>
  <td class="rank-col">{rank}</td>
  <td class="code-col">{_html(item.stock_code)}</td>
  <td class="name-col">{_html(item.stock_name)}</td>
  <td class="number-col"><span class="score {'score-high' if item.opportunity_score >= 55 else 'score-mid'}">{_html(item.opportunity_score)}</span></td>
  <td>{'<span class="badge badge-high">B</span>' if item.grade == 'B' else '<span class="badge badge-neutral">C</span>'}</td>
  <td class="number-col">{_html(item.position_score)}</td>
  <td class="number-col">{_html(item.fundamental_score)}</td>
  <td class="number-col">{_html(item.trend_score)}</td>
  <td class="number-col">{_html(item.flow_score)}</td>
  <td class="number-col">{_html(item.risk_reward_score)}</td>
  <td class="number-col">{_html(item.liquidity_score)}</td>
  <td class="number-col" title="基本面风险 {_html(item.fundamental_risk_penalty)}">{_html(item.risk_penalty)}</td>
  <td class="number-col">{_html(item.support)}</td>
  <td class="number-col">{_html(item.resistance)}</td>
  <td class="number-col">{_html(item.stop_loss)}</td>
  <td class="number-col">{_html(item.target_1)}</td>
  <td class="number-col">{_html(item.risk_reward_ratio)}</td>
  <td>{_html(item.week_trend)} / {_html(item.day_trend)}</td>
  <td class="reason-col">{_html('；'.join(item.reason[:3]) or '-')}</td>
</tr>
<tr class="detail-row">
  <td></td>
  <td colspan="18">
    <details>
      <summary>查看完整评分拆解和数据覆盖</summary>
      <div class="detail-grid">
        <div class="mini-panel">
          <h3>风险收益</h3>
          <dl class="mini-list">
            <dt>支撑</dt><dd>{_html(item.support)}</dd>
            <dt>压力</dt><dd>{_html(item.resistance)}</dd>
            <dt>止损</dt><dd>{_html(item.stop_loss)}</dd>
            <dt>目标1</dt><dd>{_html(item.target_1)}</dd>
            <dt>目标2</dt><dd>{_html(item.target_2)}</dd>
            <dt>RR</dt><dd>{_html(item.risk_reward_ratio)}</dd>
          </dl>
        </div>
        <div class="mini-panel">
          <h3>基本面</h3>
          <dl class="mini-list">
            <dt>得分</dt><dd>{_html(item.fundamental_score)} / 15</dd>
            <dt>风险扣分</dt><dd>{_html(item.fundamental_risk_penalty)}</dd>
            <dt>报告期</dt><dd>{_html(fundamental_period)}</dd>
            <dt>覆盖率</dt><dd>{_html(fundamental_coverage)}%</dd>
            <dt>上游来源</dt><dd>{_html(fundamental_sources)}</dd>
            <dt>冲突数</dt><dd>{_html(len(fundamental.get('conflicts') or []))}</dd>
          </dl>
        </div>
        <div class="mini-panel">
          <h3>数据覆盖</h3>
          <dl class="mini-list">
            <dt>Quote</dt><dd>{_html(_data_status(coverage.quote))}</dd>
            <dt>日K</dt><dd>{_html(_data_status(coverage.day_kline))}</dd>
            <dt>周K</dt><dd>{_html(_data_status(coverage.week_kline, '上游暂不可用'))}</dd>
            <dt>基本面</dt><dd>{_html(_data_status(coverage.fundamental, '覆盖不足'))}</dd>
            <dt>催化</dt><dd>Phase 3 待接入</dd>
            <dt>其他源</dt><dd>{_html(planned_missing)}</dd>
          </dl>
        </div>
        <div class="mini-panel">
          <h3>公式</h3>
          <p class="formula-text">{_html(item.score_formula)}</p>
        </div>
      </div>
      <div class="table-shell nested-table">
        <table>
          <thead><tr><th>模块</th><th>得分</th><th>上限</th><th>覆盖</th><th>原因</th><th>原始值</th></tr></thead>
          <tbody>{score_parts}</tbody>
        </table>
      </div>
      <div class="table-shell nested-table">
        <table>
          <thead><tr><th>基本面字段</th><th>值</th><th>数据源</th><th>真实上游</th><th>报告期</th><th>覆盖</th><th>置信度</th><th>错误</th></tr></thead>
          <tbody>{fundamental_rows or '<tr><td colspan="8">基本面核心字段覆盖不足</td></tr>'}</tbody>
        </table>
      </div>
    </details>
  </td>
</tr>
"""
        )
    coverage_pct = round(result.coverage.coverage_rate * 100, 2)
    body = (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>V2 机会扫描</title>"
        "<style>"
        ":root{color-scheme:light;--bg:#f6f8fa;--surface:#fff;--surface-muted:#f8fafc;--text:#1f2937;--muted:#667085;--border:#e5e7eb;--primary:#1d4ed8;--up:#d92d20;--down:#07883f;--shadow:0 4px 18px rgba(15,23,42,.06)}"
        "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}"
        "a{color:var(--primary);text-decoration:none}.dashboard{max-width:1440px;margin:0 auto;padding:20px}h1,h2,h3{margin:0;line-height:1.25}h1{font-size:28px}h2{font-size:20px}.section{margin-top:20px}"
        ".card{background:var(--surface);border:1px solid rgba(229,231,235,.9);border-radius:12px;box-shadow:var(--shadow);padding:20px}.status-bar{display:flex;justify-content:space-between;gap:20px}.eyebrow{color:var(--primary);font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.snapshot-note,.muted{color:var(--muted)}"
        ".status-actions{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}.btn{display:inline-flex;align-items:center;min-height:36px;padding:7px 12px;border:1px solid var(--border);border-radius:8px;background:#fff;font-weight:650}.btn-primary{color:#fff;background:var(--primary);border-color:var(--primary)}"
        ".badge{display:inline-flex;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:800;white-space:nowrap}.badge-high{color:#067647;background:#ecfdf3}.badge-neutral{color:#475467;background:#f2f4f7}.badge-low{color:#b42318;background:#fef3f2}"
        ".grid{display:grid;gap:14px}.stat-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.metric-label{color:var(--muted);font-size:13px}.metric-value{margin-top:6px;font-size:26px;font-weight:780}.notice{margin-top:14px;padding:10px 12px;border-left:4px solid #f79009;border-radius:7px;background:#fffaeb;color:#7a2e0e}"
        ".section-heading{display:flex;align-items:end;justify-content:space-between;gap:16px;margin-bottom:12px}.table-shell{width:100%;overflow:auto;border:1px solid var(--border);border-radius:10px}table{width:100%;border-collapse:separate;border-spacing:0;font-variant-numeric:tabular-nums}th,td{padding:10px 11px;border-bottom:1px solid var(--border);text-align:left;vertical-align:middle}thead th{position:sticky;top:0;z-index:3;color:#344054;background:#f2f4f7;font-size:12px;white-space:nowrap}tbody tr:nth-child(4n+1),tbody tr:nth-child(4n+2){background:#fbfcfd}tbody tr:hover{background:#f0f5ff}"
        ".rank-col{position:sticky;left:0;z-index:2;width:48px;min-width:48px;text-align:center;background:inherit}.code-col{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap}.name-col{min-width:90px;white-space:nowrap;font-weight:700}.number-col{text-align:right;white-space:nowrap}.reason-col{min-width:220px;max-width:360px;white-space:normal}.score{display:inline-flex;min-width:48px;justify-content:center;border-radius:7px;padding:3px 7px;font-weight:800}.score-high{color:#175cd3;background:#eff8ff}.score-mid{color:#475467;background:#f2f4f7}.detail-row td{background:#fff}.detail-row details{color:var(--muted)}.detail-row summary{cursor:pointer;font-weight:700;color:#344054}.detail-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:12px 0}.mini-panel{padding:13px;border:1px solid var(--border);border-radius:9px;background:var(--surface-muted)}.mini-panel h3{font-size:14px;margin-bottom:8px}.mini-list{display:grid;grid-template-columns:1fr auto;gap:5px 12px;margin:0}.mini-list dd{margin:0;color:var(--text)}.formula-text{margin:0;color:#475467}.nested-table{margin-top:10px}.nested-table code{white-space:normal;word-break:break-word}.footer-card{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:24px}"
        "@media(max-width:900px){.dashboard{padding:12px}.status-bar,.footer-card{flex-direction:column}.stat-grid,.detail-grid{grid-template-columns:1fr}.status-actions{justify-content:flex-start}}"
        "</style></head><body><main class=\"dashboard\">"
        "<header class=\"card\"><div class=\"status-bar\"><div>"
        "<div class=\"eyebrow\">机会发现 · Phase 2A</div><h1>scan_mainboard V2 Top30</h1>"
        "<p class=\"snapshot-note\">技术评分保持 Phase1 原样，新增真实基本面评分与财务风险。</p></div>"
        f"<div class=\"status-actions\"><span class=\"badge badge-neutral\">{_html(result.score_version)}</span><a class=\"btn\" href=\"/gpt/{_html(secret)}/scan/v2\">JSON</a><a class=\"btn btn-primary\" href=\"/gpt/{_html(secret)}/scan/ab\">V1 vs V2</a></div></div>"
        "<div class=\"notice\">Phase2A 已接入财务、估值和业绩预告/快报。催化、新闻、政策和主力资金仍留待 Phase3；字段缺失不会按 0 分处理。</div></header>"
        "<section class=\"section grid stat-grid\">"
        f"<article class=\"card\"><div class=\"metric-label\">候选池</div><div class=\"metric-value\">{_html(result.candidate_pool_size)}</div></article>"
        f"<article class=\"card\"><div class=\"metric-label\">行情覆盖</div><div class=\"metric-value\">{coverage_pct}%</div></article>"
        f"<article class=\"card\"><div class=\"metric-label\">过滤后主板</div><div class=\"metric-value\">{_html(result.coverage.filtered_mainboard)}</div></article>"
        f"<article class=\"card\"><div class=\"metric-label\">扫描耗时</div><div class=\"metric-value\">{_html(result.duration_seconds)}s</div></article>"
        "</section>"
        "<section class=\"section card\"><div class=\"section-heading\"><div><h2>Top30</h2><p class=\"muted\">raw_top30 完全按 opportunity_score 排序</p></div>"
        "<details><summary>公式与缺失数据</summary>"
        f"<p class=\"muted\">{_html(result.score_formula)}</p><p class=\"muted\">后续阶段待接入：{_html(_planned_source_labels(result.missing_data_sources))}</p>"
        "<p class=\"muted\">当前上游：周 K 接口若失败，系统会降级使用日 K 趋势并标记周 K 暂不可用。</p></details></div>"
        "<div class=\"table-shell\"><table><thead><tr><th class=\"rank-col\">#</th><th>代码</th><th>名称</th><th>机会分</th><th>等级</th>"
        "<th>位置</th><th>基本面</th><th>趋势</th><th>资金量价</th><th>RR分</th><th>流动性</th><th>风险扣分</th>"
        "<th>支撑</th><th>压力</th><th>止损</th><th>目标1</th><th>RR</th><th>周/日趋势</th><th>理由</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
        "<footer class=\"section card footer-card\"><span class=\"muted\">V2 仍是观察清单，不是交易建议。</span>"
        f"<a class=\"btn btn-primary\" href=\"/gpt/{_html(secret)}/scan/v2/html?top_n=30&pool_size={_html(result.candidate_pool_size)}\">刷新</a></footer>"
        "</main></body></html>"
    )
    return HTMLResponse(body, headers=NO_CACHE_HEADERS)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/providers")
async def provider_health():
    return {
        "status": "ok",
        **container.market_data.health(),
        "fundamentals": container.fundamentals.health(),
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


@router.get("/fundamental/{code}")
async def fundamental(code: str):
    return await container.fundamentals.get(code)


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


@router.get("/gpt/{secret}/stock/{code}/fundamental", dependencies=[Depends(require_web_secret)])
async def gpt_fundamental(secret: str, code: str):
    return web_response(await container.fundamentals.get(code))


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
    if (top_n, pool_size, min_amount) == (30, 420, 50_000_000):
        state = v2_dashboard_cache.get()
        if state.result is None:
            return HTMLResponse(
                "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta http-equiv=\"refresh\" content=\"3\"><title>V2 机会扫描初始化中</title></head>"
                "<body><h1>V2 机会扫描正在初始化</h1><p>后台正在生成首份扫描快照，请稍后自动重试。</p></body></html>",
                status_code=503,
                headers=NO_CACHE_HEADERS,
            )
        result = state.result
    else:
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
