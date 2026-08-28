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
from pathlib import Path
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
ASSET_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_TEMPLATE = (ASSET_ROOT / "templates" / "live_dashboard.html").read_text(encoding="utf-8")
LIVE_CSS = (ASSET_ROOT / "static" / "css" / "live.css").read_text(encoding="utf-8")
LIVE_JS = (ASSET_ROOT / "static" / "js" / "live.js").read_text(encoding="utf-8")


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
    coverage: Any | None = None
    industry: Any | None = None
    concept: Any | None = None
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
        loader: Callable[[], Awaitable[tuple[Any, ...]]],
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
            loaded = await self.loader()
            if len(loaded) == 3:
                market, scan, quotes = loaded
                coverage = industry = concept = None
            else:
                market, scan, quotes, coverage, industry, concept = loaded
            cached_at = now_shanghai()
            snapshot = LiveSnapshot(
                market=market,
                scan=scan,
                quotes=quotes,
                snapshot_time=cached_at,
                coverage=coverage,
                industry=industry,
                concept=concept,
            )
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
        return "—"
    if isinstance(value, datetime):
        value = value.isoformat()
    return html.escape(str(value), quote=True)


def _number(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def _compact_number(value: float | int | None, *, volume: bool = False) -> str:
    if value is None:
        return "—"
    absolute = abs(value)
    if not volume and absolute >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f} 万亿"
    if absolute >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿"
    if absolute >= 10_000:
        return f"{value / 10_000:.2f} 万{'股' if volume else ''}"
    return f"{value:,.0f}" if volume else f"{value:,.2f}"


def _time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return f'<time datetime="{_escape(value.isoformat())}" title="{_escape(value.isoformat())}">{_escape(value.strftime("%Y-%m-%d %H:%M:%S"))}</time>'


def _change_class(value: float | int | None) -> str:
    if value is None or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def _badge(value: Any, category: str = "quality") -> str:
    text = "UNAVAILABLE" if value is None else str(value).upper()
    allowed = {"live", "stale", "old", "unavailable", "high", "medium", "low", "full", "broad", "partial"}
    css = text.lower() if text.lower() in allowed else "neutral"
    return f'<span class="badge badge-{css}" data-badge-category="{_escape(category)}">{_escape(text)}</span>'


def _score_class(value: float | int | None) -> str:
    if value is None or value < 70:
        return "score-low"
    if value < 80:
        return "score-mid"
    if value < 90:
        return "score-high"
    return "score-elite"


def _freshness_rows(value: Any) -> str:
    # Eastmoney f86/f124 has only been established as a provider update time.
    # It must not be presented as an exchange trade timestamp.
    semantics = "provider_update_time" if value.timestamp_source == "eastmoney" else "fetch_time"
    provider_timestamp = value.source_timestamp if value.timestamp_source == "eastmoney" else None
    return "".join(
        f"<tr><th>{html.escape(label)}</th><td>{_time(field) if isinstance(field, datetime) else _escape(field)}</td></tr>"
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
    return (
        DOCUMENT_TEMPLATE.replace("__PAGE_TITLE__", html.escape(title))
        .replace("__BODY_CLASS__", "live-dashboard")
        .replace("__LIVE_CSS__", LIVE_CSS)
        .replace("__LIVE_JS__", LIVE_JS)
        .replace("__PAGE_BODY__", body)
    )


def response(body: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status_code, headers=NO_CACHE_HEADERS)


def build_snapshot_template(snapshot: LiveSnapshot) -> str:
    market = snapshot.market
    scan = snapshot.scan
    index_labels = (("shanghai", "上证"), ("shenzhen", "深证"), ("chinext", "创业板"))
    index_cards = []
    for key, label in index_labels:
        item = market.indices.get(key)
        pct = item.pct_change if item else None
        index_cards.append(
            '<article class="card metric-card">'
            f'<div class="metric-label">{_escape(label)} <span class="index-code">{_escape(item.code if item else None)}</span></div>'
            f'<div class="index-price">{_number(item.price if item else None)}</div>'
            f'<div class="index-change {_change_class(pct)}">{("+" if pct is not None and pct > 0 else "")}{_number(pct)}%</div>'
            '</article>'
        )

    candidate_rows = []
    for rank, item in enumerate(scan.candidates, 1):
        quote = snapshot.quotes.get(item.code)
        detail_href = f"/gpt/{SECRET_MARKER}/live/{NONCE_MARKER}/stock/{item.code}"
        kline_href = f"/gpt/{SECRET_MARKER}/stock/{item.code}/kline"
        reasons = list(item.reason or [])
        reason_tags = "".join(f'<span class="reason-tag">{_escape(reason)}</span>' for reason in reasons[:3])
        full_reason = "；".join(reasons)
        pct = item.pct_change
        source = quote.source if quote else scan.source
        quality = quote.quality if quote else scan.quality
        candidate_rows.append(
            f'<tr data-rank="{rank}" data-code="{_escape(item.code)}" data-name="{_escape(item.name)}" '
            f'data-price="{item.price or 0}" data-pct="{pct or 0}" data-amount="{item.amount or 0}" '
            f'data-turnover="{item.turnover_rate or 0}" data-ratio="{item.volume_ratio or 0}" '
            f'data-score="{item.total_score or 0}" data-source="{_escape(str(source).lower())}">'
            f'<td class="rank-col">{rank}</td>'
            f'<td class="code-col"><a href="{html.escape(detail_href, quote=True)}">{_escape(item.code)}</a></td>'
            f'<td class="name-col"><a href="{html.escape(detail_href, quote=True)}">{_escape(item.name)}</a></td>'
            f'<td class="price-col">{_number(item.price)}</td>'
            f'<td class="number-col {_change_class(pct)}">{("+" if pct is not None and pct > 0 else "")}{_number(pct)}%</td>'
            f'<td class="number-col amount-col" title="{_escape(item.amount)}">{_compact_number(item.amount)}</td>'
            f'<td class="number-col">{_number(item.turnover_rate)}%</td>'
            f'<td class="number-col">{_number(item.volume_ratio)}</td>'
            f'<td class="number-col"><span class="score {_score_class(item.total_score)}">{_number(item.total_score)}</span></td>'
            f'<td>{_badge(quality)}</td>'
            f'<td class="reason-col" title="{_escape(full_reason)}"><div class="reason-tags">{reason_tags or "—"}</div>'
            + (f'<details class="reason-more"><summary>完整理由（{len(reasons)}）</summary><div class="reason-full">{_escape(full_reason)}</div></details>' if len(reasons) > 3 else '')
            + '</td>'
            f'<td class="action-col"><a href="{html.escape(detail_href, quote=True)}">查看详情</a> · '
            f'<a href="{html.escape(kline_href, quote=True)}" target="_blank" rel="noopener">K线</a> · '
            f'<a href="{html.escape(detail_href, quote=True)}" target="_blank" rel="noopener">新窗口</a></td>'
            f'<td class="optional-col number-col">{_number(quote.prev_close if quote else None)}</td>'
            f'<td class="optional-col number-col">{_number(quote.open if quote else None)}</td>'
            f'<td class="optional-col number-col">{_number(quote.high if quote else None)}</td>'
            f'<td class="optional-col number-col">{_number(quote.low if quote else None)}</td>'
            f'<td class="optional-col number-col">{_number(quote.change if quote else None)}</td>'
            f'<td class="optional-col number-col" title="{_escape(quote.volume if quote else None)}">{_compact_number(quote.volume if quote else None, volume=True)}</td>'
            f'<td class="optional-col">{_escape(source)}</td>'
            f'<td class="optional-col">{_time(quote.source_timestamp if quote else scan.source_timestamp)}</td>'
            '</tr>'
        )

    next_href = f"/gpt/{SECRET_MARKER}/live/{NONCE_MARKER}"
    coverage = snapshot.coverage
    if coverage is None:
        coverage_html = '<div class="empty-state">覆盖率统计当前不可用</div>'
    else:
        coverage_pct = max(0, min(100, coverage.coverage_rate * 100))
        filter_pairs = (
            ("创业板", coverage.excluded_chinext), ("科创板", coverage.excluded_star),
            ("北交所", coverage.excluded_bse), ("ST/退市", coverage.excluded_st),
            ("停牌", coverage.excluded_suspended), ("流动性不足", coverage.excluded_illiquid),
            ("涨跌停/一字板", coverage.excluded_limit_untradable),
        )
        filter_list = "".join(f'<dt>{_escape(key)}</dt><dd>{_number(value, 0)}</dd>' for key, value in filter_pairs)
        failure_list = "".join(f'<dt>{_escape(key)}</dt><dd>{_number(value, 0)}</dd>' for key, value in coverage.failure_sources.items()) or '<div class="empty-state">无统计</div>'
        missing_list = "".join(f'<dt>{_escape(key)}</dt><dd>{_number(value, 0)}</dd>' for key, value in coverage.missing_fields.items()) or '<div class="empty-state">无统计</div>'
        coverage_html = f"""
<div class="card coverage-card">
  <div class="coverage-hero">
    <div>
      <div class="metric-label">结构化行情覆盖率</div>
      <div class="coverage-rate">{coverage_pct:.2f}%</div>
      <div class="progress" role="progressbar" aria-valuenow="{coverage_pct:.2f}" aria-valuemin="0" aria-valuemax="100"><span style="width:{coverage_pct:.2f}%"></span></div>
      <div class="metric-sub">等级 {_badge(coverage.coverage_level, "coverage")}</div>
    </div>
    <div class="grid coverage-grid">
      <div class="metric-card"><div class="metric-label">证券总数</div><div class="metric-value">{_number(coverage.total_securities, 0)}</div></div>
      <div class="metric-card"><div class="metric-label">请求行情</div><div class="metric-value">{_number(coverage.quotes_requested, 0)}</div></div>
      <div class="metric-card"><div class="metric-label">成功</div><div class="metric-value">{_number(coverage.quotes_success, 0)}</div></div>
      <div class="metric-card"><div class="metric-label">失败</div><div class="metric-value">{_number(coverage.quotes_failed, 0)}</div></div>
      <div class="metric-card"><div class="metric-label">沪深主板过滤后</div><div class="metric-value">{_number(coverage.filtered_mainboard, 0)}</div></div>
    </div>
  </div>
  <div class="freshness-row" aria-label="数据新鲜度分布">
    <span class="summary-pill">LIVE <strong>{_number(coverage.fresh_live_count, 0)}</strong></span>
    <span class="summary-pill">STALE <strong>{_number(coverage.fresh_stale_count, 0)}</strong></span>
    <span class="summary-pill">OLD <strong>{_number(coverage.fresh_old_count, 0)}</strong></span>
    <span class="summary-pill">UNAVAILABLE <strong>{_number(coverage.unavailable_count, 0)}</strong></span>
    <span class="summary-pill">行业覆盖 <strong>{_escape(coverage.industry_success)} / {_escape(coverage.industry_total)}</strong></span>
    <span class="summary-pill">概念覆盖 <strong>{_escape(coverage.concept_success)} / {_escape(coverage.concept_total)}</strong></span>
  </div>
  <details class="compact-details">
    <summary>查看过滤、失败源与缺失字段统计</summary>
    <div class="detail-columns">
      <div class="mini-panel"><h3>过滤原因</h3><dl class="mini-list">{filter_list}</dl></div>
      <div class="mini-panel"><h3>失败源</h3><dl class="mini-list">{failure_list}</dl></div>
      <div class="mini-panel"><h3>缺失字段</h3><dl class="mini-list">{missing_list}</dl></div>
    </div>
  </details>
</div>
"""

    def sector_table(title: str, ranking: Any | None) -> str:
        if ranking is None:
            return f'<article class="card sector-card"><h2>{html.escape(title)} Top20</h2><div class="empty-state">当前不可用</div></article>'
        rows = "".join(
            f'<tr><td>{item.rank}</td><td class="name-col">{_escape(item.name)}</td>'
            f'<td class="number-col {_change_class(item.pct_change)}">{("+" if item.pct_change is not None and item.pct_change > 0 else "")}{_number(item.pct_change)}%</td>'
            f'<td class="number-col amount-col" title="{_escape(item.amount)}">{_compact_number(item.amount)}</td>'
            f'<td class="number-col">{_escape(item.up_count)}</td><td class="number-col">{_escape(item.down_count)}</td></tr>'
            for item in ranking.items[:20]
        )
        return (
            f'<article class="card sector-card"><div class="section-heading"><h2>{html.escape(title)} Top20</h2>'
            f'<span class="metric-sub">覆盖 {_escape(ranking.success_count)} / {_escape(ranking.total_count)}</span></div>'
            '<div class="table-shell"><table><thead><tr><th>排名</th><th>名称</th><th>涨跌幅</th><th>成交额</th>'
            f'<th>上涨</th><th>下跌</th></tr></thead><tbody>{rows}</tbody></table></div></article>'
        )

    sector_html = sector_table("行业", snapshot.industry) + sector_table("概念", snapshot.concept)
    provider_timestamp = market.source_timestamp if market.timestamp_source == "eastmoney" else None
    body = f"""
<header class="card">
  <div class="status-bar">
    <div><div class="eyebrow">只读实时行情快照</div><h1>GPT Market Live Dashboard</h1><p class="snapshot-note">这是快照页面，不会自动刷新，请点击按钮获取新快照。</p></div>
    <div class="status-actions">{_badge(market.quality)} {_badge(market.confidence, "confidence")}<a class="btn btn-primary" href="{html.escape(next_href, quote=True)}" title="获取最新行情快照">获取最新实时快照</a></div>
  </div>
  {WARNING_MARKER}
  <div class="status-meta">
    <span>快照时间 <strong>{_time(snapshot.snapshot_time)}</strong></span>
    <span>Provider 时间 <strong>{_time(provider_timestamp)}</strong></span>
    <span>服务器时间 <strong>{SERVER_TIME_MARKER}</strong></span>
    <span>市场 <strong>{MARKET_STATUS_MARKER}</strong></span>
    <span>缓存年龄 <strong>{AGE_MS_MARKER} ms</strong></span>
    <span>数据源 <strong>{_escape(market.source)}</strong></span>
  </div>
  <details class="compact-details"><summary>查看时间语义与快照标识</summary><div class="id-list"><span>snapshot_time: {_time(snapshot.snapshot_time)}</span><span>server_timestamp: {SERVER_TIME_MARKER}</span><span>provider_timestamp: {_time(provider_timestamp)}</span><span>fetch_timestamp: {_time(market.server_timestamp)}</span><span>market_timestamp: —</span><span>age_seconds: {_number(market.age_seconds)}</span><span>quality: {_escape(market.quality)}</span><span>confidence: {_escape(market.confidence)}</span><span>snapshot_id: {_escape(market.snapshot_id)}</span><span>scan_id: {_escape(scan.scan_id)}</span><span>stale: {STALE_MARKER}</span><span>timestamp_semantics: {"provider_update_time" if market.timestamp_source == "eastmoney" else "fetch_time"}</span></div><p class="notice">Provider 时间不等同于交易所最后成交时间；无法证明时 market_timestamp 不展示。</p></details>
</header>
<section class="section"><div class="section-heading"><h2>核心指数</h2><p>上涨红、下跌绿，遵循 A 股习惯</p></div><div class="grid index-grid">{''.join(index_cards)}</div></section>
<section class="section"><div class="section-heading"><h2>市场概览</h2></div><div class="grid stat-grid">
  <article class="card metric-card"><div class="metric-label">上涨家数</div><div class="metric-value up">{_number(market.breadth.up_count, 0)}</div></article>
  <article class="card metric-card"><div class="metric-label">下跌家数</div><div class="metric-value down">{_number(market.breadth.down_count, 0)}</div></article>
  <article class="card metric-card"><div class="metric-label">平盘家数</div><div class="metric-value">{_number(market.breadth.flat_count, 0)}</div></article>
  <article class="card metric-card"><div class="metric-label">市场成交额</div><div class="metric-value" title="{_escape(market.amount)}">{_compact_number(market.amount)}</div></article>
</div></section>
<section class="section"><div class="section-heading"><h2>覆盖率与数据质量</h2><p>用于判断本轮是否可称为全量扫描</p></div>{coverage_html}</section>
<section class="section"><div class="section-heading"><div><h2>scan_mainboard Top30</h2><p>候选详情与理由均来自当前快照，页面筛选不会重新请求行情。默认列对应 price / change_pct / amount / turnover_rate / volume_ratio / total_score。</p></div></div>
  <div class="card">
    <div class="toolbar">
      <label class="field field-search">搜索代码或名称<input id="stock-search" type="search" placeholder="例如 002284 / 亚钾国际"></label>
      <label class="field">最低评分<input id="min-score" type="number" min="0" max="100" step="1" value="0"></label>
      <label class="field">数据源<select id="source-filter"><option value="all">全部</option><option value="tencent">Tencent</option><option value="eastmoney">EastMoney</option></select></label>
      <label class="field">涨跌方向<select id="direction-filter"><option value="all">全部</option><option value="up">上涨</option><option value="down">下跌</option><option value="flat">平盘</option></select></label>
      <button id="toggle-columns" class="btn" type="button" aria-expanded="false">展开更多字段</button>
      <span id="result-count" class="result-count">显示 {len(candidate_rows)} / {len(candidate_rows)}</span>
    </div>
    <div class="table-shell"><table id="top30-table"><thead><tr>
      <th class="rank-col"><button class="sort-button" data-sort="rank">#</button></th><th><button class="sort-button" data-sort="code">代码</button></th><th><button class="sort-button" data-sort="name">名称</button></th>
      <th><button class="sort-button" data-sort="price">现价</button></th><th><button class="sort-button" data-sort="pct">涨跌幅</button></th><th><button class="sort-button" data-sort="amount">成交额</button></th>
      <th><button class="sort-button" data-sort="turnover">换手率</button></th><th><button class="sort-button" data-sort="ratio">量比</button></th><th><button class="sort-button" data-sort="score">总分</button></th><th>状态</th><th>理由</th><th>操作</th>
      <th class="optional-col">昨收 / prev_close</th><th class="optional-col">今开 / open</th><th class="optional-col">最高 / high</th><th class="optional-col">最低 / low</th><th class="optional-col">涨跌额 / change</th><th class="optional-col">成交量 / volume</th><th class="optional-col">source</th><th class="optional-col">source_time</th>
    </tr></thead><tbody>{''.join(candidate_rows)}</tbody></table></div>
  </div>
</section>
<section class="section"><div class="section-heading"><h2>行业与概念排行</h2><p>当前快照 Top20</p></div><div class="grid sector-grid">{sector_html}</div></section>
<footer class="section card footer-card"><div><h3>只读行情快照</h3><div class="footer-note">数据质量、Provider 时间语义和覆盖率以本页状态为准；页面不会自动刷新。</div></div><a class="btn btn-primary" href="{html.escape(next_href, quote=True)}" title="获取最新行情快照">刷新到最新行情（新快照）</a></footer>
"""
    return _document("GPT Market Live Dashboard", body)


def build_stock_template(quote: Any, snapshot: LiveSnapshot) -> str:
    next_href = f"/gpt/{SECRET_MARKER}/live/{NONCE_MARKER}"
    pct = quote.pct_change
    body = f"""
<header class="card">
  <div class="status-bar"><div><div class="eyebrow">个股快照</div><h1>{_escape(quote.code)} {_escape(quote.name)}</h1><p class="snapshot-note">当前页面仅展示已缓存快照，不触发外部行情采集。</p></div><div class="status-actions">{_badge(quote.quality)} {_badge(quote.confidence, "confidence")}<a class="btn btn-primary" href="{html.escape(next_href, quote=True)}">返回最新行情快照</a></div></div>
  {WARNING_MARKER}
  <div class="status-meta"><span>快照时间 <strong>{_time(snapshot.snapshot_time)}</strong></span><span>服务器时间 <strong>{SERVER_TIME_MARKER}</strong></span><span>缓存年龄 <strong>{AGE_MS_MARKER} ms</strong></span><span>市场 <strong>{MARKET_STATUS_MARKER}</strong></span></div>
</header>
<section class="section grid stat-grid">
  <article class="card metric-card"><div class="metric-label">最新价</div><div class="metric-value">{_number(quote.price)}</div><div class="index-change {_change_class(pct)}">{("+" if pct is not None and pct > 0 else "")}{_number(pct)}%</div></article>
  <article class="card metric-card"><div class="metric-label">成交额</div><div class="metric-value" title="{_escape(quote.amount)}">{_compact_number(quote.amount)}</div></article>
  <article class="card metric-card"><div class="metric-label">换手率</div><div class="metric-value">{_number(quote.turnover_rate)}%</div></article>
  <article class="card metric-card"><div class="metric-label">量比</div><div class="metric-value">{_number(quote.volume_ratio)}</div></article>
</section>
<section class="section card"><div class="section-heading"><h2>日内行情</h2></div><div class="table-shell"><table><tbody>
  <tr><th>昨收</th><td class="number-col">{_number(quote.prev_close)}</td><th>今开</th><td class="number-col">{_number(quote.open)}</td></tr>
  <tr><th>最高</th><td class="number-col">{_number(quote.high)}</td><th>最低</th><td class="number-col">{_number(quote.low)}</td></tr>
  <tr><th>涨跌额</th><td class="number-col {_change_class(quote.change)}">{_number(quote.change)}</td><th>成交量</th><td class="number-col" title="{_escape(quote.volume)}">{_compact_number(quote.volume, volume=True)}</td></tr>
  <tr><th>数据源</th><td>{_escape(quote.source)}</td><th>Provider 时间</th><td>{_time(quote.source_timestamp)}</td></tr>
  <tr><th>bid1~bid5 / ask1~ask5</th><td colspan="3">当前共享 Provider 未采集，不推测</td></tr>
</tbody></table></div></section>
<section class="section card"><details class="compact-details"><summary>查看完整数据质量与时间语义</summary><div class="table-shell"><table><tbody>{_freshness_rows(quote)}</tbody></table></div><p class="notice">Provider 时间不等同于交易所最后成交时间；无法证明时 market_timestamp 不展示。</p></details></section>
<footer class="section card footer-card"><span class="footer-note">本页面为只读个股快照。</span><a class="btn btn-primary" href="{html.escape(next_href, quote=True)}">刷新到最新行情（新快照）</a></footer>
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
        .replace(SERVER_TIME_MARKER, _time(view.server_time))
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
