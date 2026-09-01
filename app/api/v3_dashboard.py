from __future__ import annotations

from html import escape
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.container import container
from app.utils.time import now_shanghai
from app.v3.domain.features import FeatureQuery, FeatureSortField


router = APIRouter(prefix="/v3", tags=["V3 Dashboard"])

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

SORT_LABELS = {
    FeatureSortField.RETURN_20D: "20日收益",
    FeatureSortField.RETURN_60D: "60日收益",
    FeatureSortField.POSITION_60D: "60日位置",
    FeatureSortField.AMOUNT: "成交额",
    FeatureSortField.ATR_PCT: "ATR波动",
    FeatureSortField.VOLUME_RATIO_5D: "5日量比",
    FeatureSortField.COVERAGE: "数据覆盖率",
    FeatureSortField.CODE: "证券代码",
}

FEATURE_FIELDS = (
    "market",
    "code",
    "name",
    "close",
    "return_3d",
    "return_5d",
    "return_20d",
    "return_60d",
    "position_60d",
    "atr_pct",
    "amount",
    "volume_ratio_5d",
    "coverage",
    "stale",
    "missing_fields",
)


def _text(value: Any) -> str:
    return escape("—" if value is None else str(value))


def _pct(value: Any, *, fraction: bool = False) -> str:
    if value is None:
        return "—"
    number = float(value) * (100 if fraction else 1)
    return f"{number:+.2f}%" if not fraction else f"{number:.2f}%"


def _number(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):,.{digits}f}"


def _amount(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number >= 1_0000_0000_0000:
        return f"{number / 1_0000_0000_0000:.2f}万亿"
    if number >= 1_0000_0000:
        return f"{number / 1_0000_0000:.2f}亿"
    if number >= 1_0000:
        return f"{number / 1_0000:.2f}万"
    return f"{number:,.0f}"


def _return_cell(value: Any) -> str:
    if value is None:
        return '<td class="num muted">—</td>'
    number = float(value)
    css = "up" if number > 0 else "down" if number < 0 else "flat"
    return f'<td class="num {css}">{_pct(number, fraction=True)}</td>'


def _fact_rows(title: str, facts: dict[str, Any]) -> str:
    if not facts:
        return f'<section class="card fact-card"><h3>{escape(title)}</h3><p class="muted">暂无数据</p></section>'
    rows = "".join(
        f"<div><span>{escape(str(key))}</span><strong>{_text(value)}</strong></div>"
        for key, value in facts.items()
    )
    return f'<section class="card fact-card"><h3>{escape(title)}</h3>{rows}</section>'


def _document(title: str, body: str, *, refresh_seconds: int | None = None) -> str:
    refresh = (
        f'<meta http-equiv="refresh" content="{refresh_seconds}">'
        if refresh_seconds is not None
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>{escape(title)}</title><style>
:root{{--bg:#f5f7fa;--card:#fff;--text:#172033;--muted:#667085;--line:#e7ebf0;--primary:#2563eb;--up:#d92d20;--down:#079455;--warn:#dc6803}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
a{{color:var(--primary);text-decoration:none}}.dashboard{{max-width:1400px;margin:0 auto;padding:20px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:0 3px 12px rgba(16,24,40,.04)}}
.hero{{padding:20px;display:flex;gap:18px;align-items:flex-start;justify-content:space-between}}h1,h2,h3{{margin:0;line-height:1.3}}h1{{font-size:28px}}h2{{font-size:21px}}h3{{font-size:16px}}.subtitle,.muted{{color:var(--muted)}}.subtitle{{margin:6px 0 0}}.meta{{font-size:12px;color:var(--muted);text-align:right;word-break:break-all}}
.badge{{display:inline-flex;padding:3px 9px;border-radius:999px;font-weight:700;font-size:12px;background:#ecfdf3;color:#027a48}}.badge.warn{{background:#fff4e5;color:var(--warn)}}
.stats{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-top:14px}}.stat{{padding:16px}}.stat span{{display:block;color:var(--muted)}}.stat strong{{display:block;margin-top:5px;font-size:24px}}
.facts{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}}.fact-card{{padding:16px}}.fact-card h3{{margin-bottom:9px}}.fact-card div{{display:flex;justify-content:space-between;gap:12px;border-top:1px dashed var(--line);padding:7px 0}}.fact-card div span{{color:var(--muted)}}.fact-card strong{{text-align:right}}
.section{{margin-top:14px;padding:18px}}.section-head{{display:flex;gap:16px;justify-content:space-between;align-items:flex-end;margin-bottom:14px}}.controls{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}select,input,button{{border:1px solid #cfd6df;border-radius:8px;background:#fff;padding:8px 10px;font:inherit}}button{{background:var(--primary);border-color:var(--primary);color:#fff;cursor:pointer}}
.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;table-layout:auto}}th,td{{padding:10px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}th{{position:sticky;top:0;background:#f9fafb;color:#475467;font-size:12px}}td.num,th.num{{text-align:right}}tr:hover td{{background:#fafcff}}.name{{min-width:90px;font-weight:600}}.up{{color:var(--up);font-weight:650}}.down{{color:var(--down);font-weight:650}}.flat{{color:#475467}}.missing{{max-width:220px;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}}
.foot{{padding:14px 2px;color:var(--muted);font-size:12px}}.empty{{max-width:760px;margin:12vh auto;padding:30px;text-align:center}}.empty p{{color:var(--muted)}}
@media(max-width:900px){{.dashboard{{padding:12px}}.hero,.section-head{{flex-direction:column;align-items:flex-start}}.meta{{text-align:left}}.stats{{grid-template-columns:repeat(2,minmax(0,1fr))}}.facts{{grid-template-columns:1fr}}h1{{font-size:24px}}}}
</style></head><body><main class="dashboard">{body}</main></body></html>"""


def initializing_page(message: str) -> HTMLResponse:
    body = (
        '<section class="card empty"><span class="badge warn">INITIALIZING</span>'
        '<h1 style="margin-top:12px">V3 行情看板正在准备数据</h1>'
        f'<p>{escape(message)}</p><p>页面将在 10 秒后自动重试。</p></section>'
    )
    return HTMLResponse(
        _document("V3 行情看板初始化中", body, refresh_seconds=10),
        status_code=503,
        headers=NO_CACHE_HEADERS,
    )


def render_dashboard(page, regime, *, sort_by: FeatureSortField, descending: bool, market: str | None, limit: int) -> str:
    quality = page.quality_summary
    coverage = float(quality.get("coverage", 0))
    successful = int(quality.get("successful_count", 0))
    failed = int(quality.get("failed_count", 0))
    stale_count = sum(1 for item in page.items if item.get("stale"))
    sort_options = "".join(
        f'<option value="{item.value}"{" selected" if item is sort_by else ""}>{escape(label)}</option>'
        for item, label in SORT_LABELS.items()
    )
    market_options = "".join(
        f'<option value="{value}"{" selected" if market == value else ""}>{label}</option>'
        for value, label in (("", "全部市场"), ("SH", "沪市"), ("SZ", "深市"), ("BJ", "北交所"))
    )
    direction_options = (
        '<option value="true" selected>降序</option><option value="false">升序</option>'
        if descending
        else '<option value="true">降序</option><option value="false" selected>升序</option>'
    )
    rows = []
    for rank, item in enumerate(page.items, 1):
        missing = item.get("missing_fields") or []
        rows.append(
            "<tr>"
            f'<td class="num">{rank}</td><td>{_text(item.get("market"))}</td>'
            f'<td>{_text(item.get("code"))}</td><td class="name">{_text(item.get("name"))}</td>'
            f'<td class="num">{_number(item.get("close"))}</td>'
            f'{_return_cell(item.get("return_3d"))}{_return_cell(item.get("return_5d"))}'
            f'{_return_cell(item.get("return_20d"))}{_return_cell(item.get("return_60d"))}'
            f'<td class="num">{_pct(item.get("position_60d"), fraction=True)}</td>'
            f'<td class="num">{_number(item.get("volume_ratio_5d"))}</td>'
            f'<td class="num">{_pct(item.get("atr_pct"), fraction=True)}</td>'
            f'<td class="num" title="{_text(item.get("amount"))}">{_amount(item.get("amount"))}</td>'
            f'<td class="num">{_pct(item.get("coverage"), fraction=True)}</td>'
            f'<td><span class="badge{" warn" if item.get("stale") else ""}">{"STALE" if item.get("stale") else "FRESH"}</span></td>'
            f'<td class="missing" title="{_text(", ".join(missing))}">{_text(", ".join(missing))}</td>'
            "</tr>"
        )
    regime_html = ""
    if regime is not None:
        regime_html = (
            '<div class="facts">'
            + _fact_rows("市场宽度", regime.breadth)
            + _fact_rows("成交与流动性", regime.turnover)
            + _fact_rows("风险偏好事实", regime.risk_appetite_facts)
            + "</div>"
        )
    body = f"""
<section class="card hero"><div><span class="badge">V3 READ-ONLY</span><h1 style="margin-top:8px">V3 全市场行情特征看板</h1>
<p class="subtitle">展示不可变 Feature Run 的事实特征；当前排序不是统一评分，也不构成投资建议。</p></div>
<div class="meta">数据时点：{_text(page.as_of.isoformat())}<br>Feature Version：{_text(page.feature_version)}<br>Run ID：{_text(page.feature_run_id)}</div></section>
<div class="stats"><section class="card stat"><span>证券总数</span><strong>{page.total_count:,}</strong></section>
<section class="card stat"><span>成功</span><strong>{successful:,}</strong></section><section class="card stat"><span>失败</span><strong>{failed:,}</strong></section>
<section class="card stat"><span>覆盖率</span><strong>{coverage * 100:.2f}%</strong></section><section class="card stat"><span>当前页陈旧</span><strong>{stale_count}</strong></section></div>
{regime_html}
<section class="card section"><div class="section-head"><div><h2>全市场事实特征</h2><p class="subtitle">当前按“{escape(SORT_LABELS[sort_by])}”{'降序' if descending else '升序'}展示，最多读取 100 条。</p></div>
<form class="controls" method="get"><select name="market">{market_options}</select><select name="sort_by">{sort_options}</select>
<select name="descending">{direction_options}</select><input name="limit" type="number" min="20" max="100" value="{limit}" aria-label="显示数量"><button type="submit">应用</button></form></div>
<div class="table-wrap"><table><thead><tr><th class="num">#</th><th>市场</th><th>代码</th><th>名称</th><th class="num">收盘价</th><th class="num">3日</th><th class="num">5日</th><th class="num">20日</th><th class="num">60日</th><th class="num">60日位置</th><th class="num">5日量比</th><th class="num">ATR%</th><th class="num">成交额</th><th class="num">覆盖</th><th>状态</th><th>缺失字段</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<footer class="foot">服务器时间：{escape(now_shanghai().isoformat())} · <a href="/api/v3/universe/features">查看 JSON</a> · <a href="/docs">API 文档</a></footer>
"""
    return _document("V3 全市场行情特征看板", body)


@router.get("/dashboard", response_class=HTMLResponse)
async def v3_dashboard(
    market: str | None = Query(default=None, pattern=r"^(SH|SZ|BJ)$"),
    sort_by: FeatureSortField = FeatureSortField.RETURN_20D,
    descending: bool = True,
    limit: int = Query(default=50, ge=20, le=100),
):
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    async with container.v3.uow() as uow:
        page = await uow.features.query(
            FeatureQuery(
                market=market,
                sort_by=sort_by,
                descending=descending,
                fields=FEATURE_FIELDS,
                limit=limit,
            )
        )
        regime = await uow.features.latest_regime()
    if page is None:
        return initializing_page("生产库尚未发布 Feature Run；后台数据任务完成后即可展示。")
    return HTMLResponse(
        render_dashboard(
            page,
            regime,
            sort_by=sort_by,
            descending=descending,
            market=market,
            limit=limit,
        ),
        headers=NO_CACHE_HEADERS,
    )
