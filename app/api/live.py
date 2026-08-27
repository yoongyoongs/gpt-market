from __future__ import annotations

import html
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from fastapi.responses import HTMLResponse


NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def nonce() -> str:
    """Return a cryptographically random URL component for every generated link."""
    return secrets.token_urlsafe(24)


def _escape(value: Any) -> str:
    if value is None:
        return "不可用"
    if isinstance(value, datetime):
        value = value.isoformat()
    return html.escape(str(value), quote=True)


def _number(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "不可用"
    return f"{value:,.{digits}f}"


def _freshness_rows(value: Any) -> str:
    # Eastmoney f86/f124 has only been established as a provider update time.
    # It must not be presented as an exchange trade timestamp.
    semantics = "provider_update_time" if value.timestamp_source == "eastmoney" else "fetch_time"
    provider_timestamp = value.source_timestamp if value.timestamp_source == "eastmoney" else None
    return "".join(
        f"<tr><th>{html.escape(label)}</th><td>{_escape(field)}</td></tr>"
        for label, field in (
            ("market_timestamp", None),
            ("provider_timestamp", provider_timestamp),
            ("fetch_timestamp", value.server_timestamp),
            ("server_timestamp", value.server_timestamp),
            ("timestamp_semantics", semantics),
            ("age_seconds", value.age_seconds),
            ("quality", value.quality),
            ("confidence", value.confidence),
            ("snapshot_id", value.snapshot_id),
        )
    )


def _document(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #1f2937; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1rem 0 2rem; }}
    th, td {{ border: 1px solid #d1d5db; padding: .5rem; text-align: left; }}
    th {{ background: #f3f4f6; }}
    .notice {{ background: #fff7ed; border-left: 4px solid #f59e0b; padding: .75rem; }}
    .refresh {{ display: inline-block; margin: 1rem 0; font-weight: 700; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


def response(body: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status_code, headers=NO_CACHE_HEADERS)


def landing_page(secret: str) -> HTMLResponse:
    href = f"/gpt/{secret}/live/{nonce()}"
    body = (
        "<h1>A 股 Live Refresh</h1>"
        "<p>点击下方的唯一链接获取当时的最新事实行情。</p>"
        f'<a class="refresh" href="{html.escape(href, quote=True)}">获取最新行情快照</a>'
    )
    return response(_document("A 股 Live Refresh", body))


def snapshot_page(secret: str, market: Any, scan: Any) -> HTMLResponse:
    index_labels = (("shanghai", "上证"), ("shenzhen", "深证"), ("chinext", "创业板"))
    index_rows = []
    for key, label in index_labels:
        item = market.indices.get(key)
        index_rows.append(
            f"<tr><td>{label}</td><td>{_escape(item.code if item else None)}</td>"
            f"<td>{_number(item.price if item else None)}</td>"
            f"<td>{_number(item.pct_change if item else None)}%</td></tr>"
        )

    candidate_rows = []
    for rank, item in enumerate(scan.candidates, 1):
        detail_href = f"/gpt/{secret}/live/{nonce()}/stock/{item.code}"
        candidate_rows.append(
            f'<tr><td>{rank}</td><td><a href="{html.escape(detail_href, quote=True)}">{_escape(item.code)}</a></td>'
            f"<td>{_escape(item.name)}</td><td>{_number(item.price)}</td><td>{_number(item.pct_change)}%</td>"
            f"<td>{_number(item.amount)}</td><td>{_number(item.turnover_rate)}%</td>"
            f"<td>{_number(item.volume_ratio)}</td><td>{_number(item.total_score)}</td>"
            f"<td>{_escape('；'.join(item.reason))}</td></tr>"
        )

    next_href = f"/gpt/{secret}/live/{nonce()}"
    body = f"""
<h1>A 股最新行情快照</h1>
<p class="notice">时间语义说明：东方财富 f86/f124 仅按 provider_update_time 展示，不能证明为交易所最后成交时间；因此 market_timestamp 显示为不可用。</p>
<h2>市场快照元数据</h2>
<table><tbody>{_freshness_rows(market)}</tbody></table>
<h2>扫描快照元数据</h2>
<table><tbody>{_freshness_rows(scan)}<tr><th>scan_id</th><td>{_escape(scan.scan_id)}</td></tr></tbody></table>
<h2>主要指数</h2>
<table><thead><tr><th>指数</th><th>代码</th><th>点位</th><th>涨跌幅</th></tr></thead><tbody>{''.join(index_rows)}</tbody></table>
<h2>市场概览</h2>
<table><tbody>
<tr><th>上涨家数</th><td>{market.breadth.up_count}</td></tr>
<tr><th>下跌家数</th><td>{market.breadth.down_count}</td></tr>
<tr><th>平盘家数</th><td>{market.breadth.flat_count}</td></tr>
<tr><th>市场成交额（元）</th><td>{_number(market.amount)}</td></tr>
</tbody></table>
<h2>scan_mainboard Top30</h2>
<table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>价格</th><th>涨跌幅</th><th>成交额</th><th>换手率</th><th>量比</th><th>总分</th><th>理由</th></tr></thead>
<tbody>{''.join(candidate_rows)}</tbody></table>
<a class="refresh" href="{html.escape(next_href, quote=True)}">获取最新行情快照</a>
"""
    return response(_document("A 股最新行情快照", body))


def stock_page(secret: str, quote: Any) -> HTMLResponse:
    next_href = f"/gpt/{secret}/live/{nonce()}"
    body = f"""
<h1>{_escape(quote.code)} {_escape(quote.name)}</h1>
<p class="notice">时间语义说明：东方财富 f86/f124 仅按 provider_update_time 展示，不能证明为交易所最后成交时间；因此 market_timestamp 显示为不可用。</p>
<table><tbody>{_freshness_rows(quote)}</tbody></table>
<table><tbody>
<tr><th>最新价</th><td>{_number(quote.price)}</td></tr>
<tr><th>涨跌幅</th><td>{_number(quote.pct_change)}%</td></tr>
<tr><th>成交额（元）</th><td>{_number(quote.amount)}</td></tr>
<tr><th>换手率</th><td>{_number(quote.turnover_rate)}%</td></tr>
<tr><th>量比</th><td>{_number(quote.volume_ratio)}</td></tr>
</tbody></table>
<a class="refresh" href="{html.escape(next_href, quote=True)}">获取最新行情快照</a>
"""
    return response(_document(f"{quote.code} {quote.name}", body))


async def html_or_error(factory: Callable[[], Awaitable[HTMLResponse]]) -> HTMLResponse:
    try:
        return await factory()
    except Exception as exc:
        body = (
            "<h1>行情暂时不可用</h1>"
            f"<p>{html.escape(str(exc))}</p>"
            "<p>没有使用历史值或推测值替代实时事实。</p>"
        )
        return response(_document("行情暂时不可用", body), status_code=503)
