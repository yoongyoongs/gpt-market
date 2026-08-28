from __future__ import annotations

import html
import asyncio
import contextlib
import logging
import re
import secrets
from dataclasses import dataclass, field, replace
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import perf_counter
from typing import Any

from fastapi.responses import HTMLResponse

from app.utils.time import SHANGHAI, now_shanghai


# Reuse Uvicorn's configured logger so INFO refresh metrics are visible in the
# container log without adding a second logging configuration.
logger = logging.getLogger("uvicorn.error")


NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

SECRET_MARKER = "__LIVE_SECRET__"
NONCE_MARKER = "__LIVE_NONCE__"
SERVER_TIME_MARKER = "__LIVE_SERVER_TIME__"
AGE_MS_MARKER = "__LIVE_AGE_MS__"
MARKET_STATUS_MARKER = "__LIVE_MARKET_STATUS__"
STALE_MARKER = "__LIVE_STALE__"
WARNING_MARKER = "__LIVE_WARNING__"
NONCE_PATTERN = re.compile(re.escape(NONCE_MARKER))


def nonce() -> str:
    """Return a cryptographically random URL component for every generated link."""
    return secrets.token_urlsafe(24)


def market_status(at: datetime | None = None) -> str:
    current = (at or now_shanghai()).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return "CLOSED"
    minute = current.hour * 60 + current.minute
    return "TRADING" if 570 <= minute <= 690 or 780 <= minute <= 900 else "CLOSED"


@dataclass(frozen=True)
class LiveSnapshot:
    market: Any
    scan: Any
    quotes: dict[str, Any]
    snapshot_time: datetime
    html_template: str = ""
    stock_html_templates: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveCacheState:
    snapshot: LiveSnapshot | None
    last_error: str | None


@dataclass(frozen=True)
class LiveSnapshotView:
    snapshot: LiveSnapshot | None
    server_time: datetime
    age_ms: int | None
    stale: bool
    warning: str | None
    status: str


class LiveSnapshotCache:
    """Non-blocking read cache refreshed by one background task."""

    def __init__(
        self,
        loader: Callable[[], Awaitable[tuple[Any, Any, dict[str, Any]]]],
        *,
        trading_interval: float = 2.0,
        closed_interval: float = 30.0,
    ) -> None:
        self.loader = loader
        self.trading_interval = trading_interval
        self.closed_interval = closed_interval
        self._state = LiveCacheState(snapshot=None, last_error=None)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def get(self) -> LiveSnapshotView:
        server_time = now_shanghai()
        state = self._state
        snapshot = state.snapshot
        age_ms = None if snapshot is None else max(0, int((server_time - snapshot.snapshot_time).total_seconds() * 1000))
        return LiveSnapshotView(
            snapshot=snapshot,
            server_time=server_time,
            age_ms=age_ms,
            stale=snapshot is not None and state.last_error is not None,
            warning="latest refresh failed, returning last successful snapshot" if snapshot is not None and state.last_error else None,
            status="INITIALIZING" if snapshot is None else market_status(server_time),
        )

    async def refresh_once(self) -> None:
        started = perf_counter()
        logger.info("行情刷新开始")
        try:
            market, scan, quotes = await self.loader()
            cached_at = now_shanghai()
            snapshot = LiveSnapshot(market=market, scan=scan, quotes=quotes, snapshot_time=cached_at)
            # Serialization is CPU-only and is completed before publication.
            # The live state becomes visible through one atomic reference swap.
            html_template, stock_templates = await asyncio.to_thread(build_snapshot_templates, snapshot)
            snapshot = replace(snapshot, html_template=html_template, stock_html_templates=stock_templates)
            self._state = LiveCacheState(snapshot=snapshot, last_error=None)
            logger.info("刷新成功股票数量=%d", len(quotes) or len(scan.candidates))
            logger.info("当前缓存时间=%s", cached_at.isoformat())
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            state = self._state
            self._state = LiveCacheState(snapshot=state.snapshot, last_error=error)
            logger.warning("刷新失败原因=%s", error)
        finally:
            logger.info("行情刷新耗时=%.3fs", perf_counter() - started)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            await self.refresh_once()
            interval = self.trading_interval if market_status() == "TRADING" else self.closed_interval
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="live-market-refresh")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


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


def build_snapshot_template(snapshot: LiveSnapshot) -> str:
    market = snapshot.market
    scan = snapshot.scan
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
        quote = snapshot.quotes.get(item.code)
        detail_href = f"/gpt/{SECRET_MARKER}/live/{NONCE_MARKER}/stock/{item.code}"
        candidate_rows.append(
            f'<tr><td>{rank}</td><td><a href="{html.escape(detail_href, quote=True)}">{_escape(item.code)}</a></td>'
            f"<td>{_escape(item.name)}</td><td>{_number(item.price)}</td>"
            f"<td>{_number(quote.prev_close if quote else None)}</td><td>{_number(quote.open if quote else None)}</td>"
            f"<td>{_number(quote.high if quote else None)}</td><td>{_number(quote.low if quote else None)}</td>"
            f"<td>{_number(quote.change if quote else None)}</td><td>{_number(item.pct_change)}%</td>"
            f"<td>{_number(quote.volume if quote else None, 0)}</td><td>{_number(item.amount)}</td>"
            f"<td>{_number(item.turnover_rate)}%</td>"
            f"<td>{_number(item.volume_ratio)}</td><td>{_number(item.total_score)}</td>"
            f"<td>{_escape(quote.source if quote else scan.source)}</td>"
            f"<td>{_escape(quote.source_timestamp if quote else scan.source_timestamp)}</td>"
            f"<td>{_escape('；'.join(item.reason))}</td></tr>"
        )

    next_href = f"/gpt/{SECRET_MARKER}/live/{NONCE_MARKER}"
    body = f"""
<h1>A 股最新行情快照</h1>
{WARNING_MARKER}
<h2>Live 缓存状态</h2>
<table><tbody>
<tr><th>ok</th><td>true</td></tr>
<tr><th>snapshot_time</th><td>{_escape(snapshot.snapshot_time)}</td></tr>
<tr><th>server_time</th><td>{SERVER_TIME_MARKER}</td></tr>
<tr><th>age_ms</th><td>{AGE_MS_MARKER}</td></tr>
<tr><th>market_status</th><td>{MARKET_STATUS_MARKER}</td></tr>
<tr><th>stale</th><td>{STALE_MARKER}</td></tr>
</tbody></table>
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
<table><thead><tr><th>#</th><th>code</th><th>name</th><th>price</th><th>prev_close</th><th>open</th><th>high</th><th>low</th><th>change</th><th>change_pct</th><th>volume</th><th>amount</th><th>换手率</th><th>量比</th><th>总分</th><th>source</th><th>source_time</th><th>理由</th></tr></thead>
<tbody>{''.join(candidate_rows)}</tbody></table>
<a class="refresh" href="{html.escape(next_href, quote=True)}">获取最新行情快照</a>
"""
    return _document("A 股最新行情快照", body)


def build_stock_template(quote: Any, snapshot: LiveSnapshot) -> str:
    next_href = f"/gpt/{SECRET_MARKER}/live/{NONCE_MARKER}"
    body = f"""
<h1>{_escape(quote.code)} {_escape(quote.name)}</h1>
{WARNING_MARKER}
<p>snapshot_time: {_escape(snapshot.snapshot_time)}；server_time: {SERVER_TIME_MARKER}；age_ms: {AGE_MS_MARKER}；market_status: {MARKET_STATUS_MARKER}；stale: {STALE_MARKER}</p>
<p class="notice">时间语义说明：东方财富 f86/f124 仅按 provider_update_time 展示，不能证明为交易所最后成交时间；因此 market_timestamp 显示为不可用。</p>
<table><tbody>{_freshness_rows(quote)}</tbody></table>
<table><tbody>
<tr><th>最新价</th><td>{_number(quote.price)}</td></tr>
<tr><th>昨收</th><td>{_number(quote.prev_close)}</td></tr>
<tr><th>今开</th><td>{_number(quote.open)}</td></tr>
<tr><th>最高</th><td>{_number(quote.high)}</td></tr>
<tr><th>最低</th><td>{_number(quote.low)}</td></tr>
<tr><th>涨跌额</th><td>{_number(quote.change)}</td></tr>
<tr><th>涨跌幅</th><td>{_number(quote.pct_change)}%</td></tr>
<tr><th>成交量（股）</th><td>{_number(quote.volume, 0)}</td></tr>
<tr><th>成交额（元）</th><td>{_number(quote.amount)}</td></tr>
<tr><th>换手率</th><td>{_number(quote.turnover_rate)}%</td></tr>
<tr><th>量比</th><td>{_number(quote.volume_ratio)}</td></tr>
<tr><th>source</th><td>{_escape(quote.source)}</td></tr>
<tr><th>source_time</th><td>{_escape(quote.source_timestamp)}</td></tr>
<tr><th>bid1~bid5 / ask1~ask5</th><td>当前共享 Provider 未采集，不推测</td></tr>
</tbody></table>
<a class="refresh" href="{html.escape(next_href, quote=True)}">获取最新行情快照</a>
"""
    return _document(f"{quote.code} {quote.name}", body)


def build_snapshot_templates(snapshot: LiveSnapshot) -> tuple[str, dict[str, str]]:
    return (
        build_snapshot_template(snapshot),
        {code: build_stock_template(quote, snapshot) for code, quote in snapshot.quotes.items()},
    )


def render_cached_template(template: str, secret: str, view: LiveSnapshotView) -> str:
    warning = f'<p class="notice">{_escape(view.warning)}</p>' if view.warning else ""
    rendered = (
        template.replace(SECRET_MARKER, html.escape(secret, quote=True))
        .replace(SERVER_TIME_MARKER, _escape(view.server_time))
        .replace(AGE_MS_MARKER, _escape(view.age_ms))
        .replace(MARKET_STATUS_MARKER, _escape(view.status))
        .replace(STALE_MARKER, str(view.stale).lower())
        .replace(WARNING_MARKER, warning)
    )
    return NONCE_PATTERN.sub(lambda _: nonce(), rendered)


def cached_response(template: str, secret: str, view: LiveSnapshotView, *, status_code: int = 200) -> HTMLResponse:
    headers = {**NO_CACHE_HEADERS, "X-Live-Cache": "HIT", "X-Live-Snapshot-Age-Ms": str(view.age_ms or 0)}
    return HTMLResponse(render_cached_template(template, secret, view), status_code=status_code, headers=headers)


def initializing_page() -> HTMLResponse:
    body = (
        "<h1>INITIALIZING</h1>"
        "<p>ok: false</p>"
        "<p>market snapshot is initializing</p>"
    )
    headers = {**NO_CACHE_HEADERS, "X-Live-Cache": "MISS"}
    return HTMLResponse(_document("行情快照初始化中", body), status_code=503, headers=headers)


def unavailable_stock_page(secret: str, code: str, view: LiveSnapshotView) -> HTMLResponse:
    next_href = f"/gpt/{secret}/live/{nonce()}"
    body = (
        f"<h1>{_escape(code)} 暂无缓存详情</h1>"
        f"<p>当前快照没有该股票；请求未触发外部行情采集。缓存 age_ms: {_escape(view.age_ms)}</p>"
        f'<a class="refresh" href="{html.escape(next_href, quote=True)}">获取最新行情快照</a>'
    )
    return response(_document("暂无缓存详情", body), status_code=404)


def log_live_response(started: float) -> None:
    logger.info("/live接口响应耗时=%.3fms", (perf_counter() - started) * 1000)
