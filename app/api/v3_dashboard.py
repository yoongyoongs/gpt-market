from __future__ import annotations

from html import escape
from types import SimpleNamespace
from typing import Any
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.container import container
from app.utils.time import SHANGHAI, now_shanghai
from app.v3.application.market_intraday_status import MarketIntradayStatusService
from app.v3.application.pipeline_eod_latest import PipelineEodLatestService
from app.v3.domain.features import FeatureQuery, FeatureSortField
from app.v3.infrastructure.providers.exchange_calendar import (
    ExchangeCalendarsAShareCalendar,
)
from app.v3.presentation import zh_cn_labels as zh_labels
from app.v3.presentation.zh_cn_labels import (
    DROP_REASON_LABELS,
    ERROR_KIND_LABELS,
    EXPERT_DISPLAY_ORDER,
    EXPERT_LABELS,
    FIELD_LABELS,
    JOB_LABELS,
    LIVE_STATUS_FIELD_LABELS,
    MARKET_LABELS,
    NAME_MISSING_LABEL,
    REGIME_FACT_LABELS,
    REGIME_SECTION_LABELS,
    SESSION_LABELS,
    STAGE_LABELS,
    STAGE_PASS_NOTES,
    STATUS_LABELS,
    SUPPORT_NOTE_LABEL,
)


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
.badge{{display:inline-flex;padding:3px 9px;border-radius:999px;font-weight:700;font-size:12px;background:#ecfdf3;color:#027a48}}.badge.warn{{background:#fff4e5;color:var(--warn)}}.badge.bad{{background:#fef3f2;color:#b42318}}.badge.info{{background:#eff8ff;color:#175cd3}}.badge.mute{{background:#f2f4f7;color:#475467}}
.stats{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-top:14px}}.stat{{padding:16px}}.stat span{{display:block;color:var(--muted)}}.stat strong{{display:block;margin-top:5px;font-size:24px}}
.facts{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}}.fact-card{{padding:16px}}.fact-card h3{{margin-bottom:9px}}.fact-card div{{display:flex;justify-content:space-between;gap:12px;border-top:1px dashed var(--line);padding:7px 0}}.fact-card div span{{color:var(--muted)}}.fact-card strong{{text-align:right}}
.section{{margin-top:14px;padding:18px}}.section-head{{display:flex;gap:16px;justify-content:space-between;align-items:flex-end;margin-bottom:14px}}.controls{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}select,input,button{{border:1px solid #cfd6df;border-radius:8px;background:#fff;padding:8px 10px;font:inherit}}button{{background:var(--primary);border-color:var(--primary);color:#fff;cursor:pointer}}
.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;table-layout:auto}}th,td{{padding:10px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}th{{position:sticky;top:0;background:#f9fafb;color:#475467;font-size:12px}}td.num,th.num{{text-align:right}}tr:hover td{{background:#fafcff}}.name{{min-width:90px;font-weight:600}}.up{{color:var(--up);font-weight:650}}.down{{color:var(--down);font-weight:650}}.flat{{color:#475467}}.missing{{max-width:220px;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}}
.foot{{padding:14px 2px;color:var(--muted);font-size:12px}}.empty{{max-width:760px;margin:12vh auto;padding:30px;text-align:center}}.empty p{{color:var(--muted)}}
@media(max-width:900px){{.dashboard{{padding:12px}}.hero,.section-head{{flex-direction:column;align-items:flex-start}}.meta{{text-align:left}}.stats{{grid-template-columns:repeat(2,minmax(0,1fr))}}.facts{{grid-template-columns:1fr}}h1{{font-size:24px}}}}
</style></head><body><main class="dashboard">{body}</main></body></html>"""


def initializing_page(message: str) -> HTMLResponse:
    body = (
        '<section class="card empty"><span class="badge warn">准备中</span>'
        '<h1 style="margin-top:12px">V3 行情看板正在准备数据</h1>'
        f'<p>{escape(message)}</p><p>页面将在 10 秒后自动重试。</p></section>'
    )
    return HTMLResponse(
        _document("V3 行情看板初始化中", body, refresh_seconds=10),
        status_code=503,
        headers=NO_CACHE_HEADERS,
    )


_STATUS_LABELS = STATUS_LABELS


def _fmt_time(value: Any) -> str:
    """§58 页面时间统一人类格式：UTC/ISO → 上海时间 `YYYY-MM-DD HH:MM:SS`；
    原始值交给调用方放进 title，不让用户读 UTC ISO。"""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _status_badge(status: str) -> str:
    """Job/事件状态 → 徽章：绿=成功/开启中、黄=跳过/部分完成/告警、红=失败。
    徽章显示中文，title 保留原始状态值便于与 API 数据对照。"""
    value = str(status or "—").upper()
    if value in ("SUCCEEDED", "COMPLETED", "ALREADY_SUCCEEDED"):
        cls = ""
    elif value in ("FAILED", "CRITICAL"):
        cls = " bad"
    elif value in ("SKIPPED", "PARTIAL", "WARNING", "LOCKED"):
        cls = " warn"
    elif value in ("OPEN", "RUNNING"):
        cls = " info"
    else:
        cls = " mute"
    label = zh_labels.zh_label(_STATUS_LABELS, value)
    if label == zh_labels.UNMAPPED_LABEL:
        label = value or "—"
    return f'<span class="badge{cls}" title="{escape(value)}">{escape(label)}</span>'


def _error_kind_label(error_summary: str | None) -> str:
    """§80 错误摘要用户可读归类：关键词 → 运行超时/数据源异常/
    数据库异常/任务执行失败；原始 exception 进 <details>。"""
    if not error_summary:
        return "—"
    text = error_summary.lower()
    if "timeout" in text or "timed out" in text:
        return ERROR_KIND_LABELS["timeout"]
    if any(word in text for word in ("connect", "http", "provider", "source", "eastmoney", "tencent")):
        return ERROR_KIND_LABELS["provider"]
    if any(word in text for word in ("database", "postgres", "sql", "deadlock", "pool", "connection refused")):
        return ERROR_KIND_LABELS["database"]
    return ERROR_KIND_LABELS["generic"]


def _fact_value(key: str, value: Any) -> str:
    """§28-§30 事实值格式化：涨跌幅/覆盖率按百分比、成交额用亿/万亿、
    布尔转是/否，其余原样（None → —）。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if key == "mean_return_3d":
        return _pct(float(value), fraction=True)
    if key == "coverage":
        return f"{float(value) * 100:.2f}%"
    if key == "total_amount":
        return _amount(value)
    if key == "advance_decline_ratio":
        return f"{float(value):.2f}"
    if isinstance(value, float):
        return _number(value)
    return _text(value)


def _fact_rows(title: str, facts: dict[str, Any], section: str | None = None) -> str:
    """§28：事实键值中文化——section 决定字段映射（observed 在
    市场宽度/成交两节含义不同）；title 保留原始字段名。"""
    if not facts:
        return f'<section class="card fact-card"><h3>{escape(title)}</h3><p class="muted">暂无数据</p></section>'
    labels = REGIME_FACT_LABELS.get(section or "", {})
    rows = "".join(
        f"<div><span title=\"{escape(str(key))}\">{escape(labels.get(str(key), str(key)))}</span>"
        f"<strong>{_fact_value(str(key), value)}</strong></div>"
        for key, value in facts.items()
    )
    return f'<section class="card fact-card"><h3>{escape(title)}</h3>{rows}</section>'


def _live_status_section(status: dict[str, Any] | None) -> str:
    """§31 Live Status → 交易时段状态：确定性规则判定（交易日历+时段），
    不含实时行情；字段名与值全部中文，原始 key/value 留在 title。"""
    if not status:
        return ""
    rows = []
    for key, value in status.items():
        if key == "source" or value is None:
            continue
        label = LIVE_STATUS_FIELD_LABELS.get(key, key)
        if key == "session":
            shown = zh_labels.zh(value, SESSION_LABELS)
            title = escape(str(value))
        elif isinstance(value, bool):
            shown = "是" if value else "否"
            title = escape(str(value))
        elif key == "known_at":
            shown = _fmt_time(value)
            title = escape(str(value))
        else:
            shown = _text(value)
            title = escape(str(value))
        rows.append(
            f'<div><span title="{title}">{escape(label)}</span><strong title="{title}">{shown}</strong></div>'
        )
    return (
        '<section class="card section"><div class="section-head"><div><h2>交易时段状态</h2>'
        '<p class="subtitle">确定性规则判定（交易日历 + 时段），不含实时行情。</p></div></div>'
        f'<div class="fact-card card">{"".join(rows)}</div></section>'
    )


def _pipeline_section(pipeline: dict[str, Any] | None) -> str:
    """§33/§80 每日任务运行状态：Job ID→中文（title 保留原始 job_id），
    错误摘要按 §80 分类显示，原始 exception 收进 <details> 技术详情。"""
    if not pipeline:
        return ""
    overall = str(pipeline.get("overall", "—"))
    jobs = pipeline.get("jobs") or {}
    rows = []
    for job_id in sorted(jobs):
        job = jobs[job_id]
        error = job.get("error_summary")
        error_kind = _error_kind_label(error)
        if error:
            error_html = (
                f"{escape(error_kind)}"
                f'<details><summary>查看技术详情</summary>'
                f'<code>{escape(str(error)[:400])}</code></details>'
            )
        else:
            error_html = "—"
        job_label = zh_labels.zh_label(JOB_LABELS, job_id)
        if job_label == zh_labels.UNMAPPED_LABEL:
            job_label = job_id
        known_at = job.get("known_at")
        rows.append(
            "<tr>"
            f'<td title="{escape(job_id)}">{escape(job_label)}</td>'
            f"<td>{_status_badge(job.get('status'))}</td>"
            f'<td class="num">{escape(str(job.get("attempt", "—")))}</td>'
            # idempotency_key 即交易日幂等键（RT-05），作为"交易日"列展示
            f"<td>{escape(str(job.get('idempotency_key', '—')))}</td>"
            f'<td class="missing">{error_html}</td>'
            f'<td title="{escape(str(known_at or ""))}">{_fmt_time(known_at)}</td>'
            "</tr>"
        )
    body = (
        "".join(rows)
        if rows
        else '<tr><td colspan="6">暂无任务运行记录（orchestrator_job_runs 为空）</td></tr>'
    )
    return (
        '<section class="card section"><div class="section-head"><div><h2>每日任务运行状态</h2>'
        f'<p class="subtitle">各任务最近一次运行状态 · 整体 {_status_badge(overall)}</p></div></div>'
        '<div class="table-wrap"><table><thead><tr><th>任务</th><th>状态</th><th class="num">尝试次数</th>'
        '<th>交易日</th><th>错误摘要</th><th>完成时间</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div></section>"
    )


def _attention_section(events: list[Any]) -> str:
    """§34 Attention → 需要关注的市场事件：只读展示客观触发事实；
    事件类型/市场中文化，未知类型兜底"其他事件"。"""
    rows = []
    for event in events[:20]:
        market = getattr(event, "market", None)
        market_label = (
            MARKET_LABELS.get(str(market), str(market)) if market else "—"
        )
        known_at = getattr(event, "known_at", None)
        rows.append(
            "<tr>"
            f'<td title="{escape(str(getattr(event, "event_type", "") or ""))}">'
            f"{escape(zh_labels.event_type_label(getattr(event, 'event_type', None)))}</td>"
            f"<td>{_status_badge(getattr(event, 'severity', ''))}</td>"
            f"<td>{escape(market_label)}</td>"
            f"<td>{escape(str(getattr(event, 'code', '') or '—'))}</td>"
            f'<td class="missing">{escape(str(getattr(event, "dedupe_key", "—")))}</td>'
            f'<td title="{escape(str(known_at or ""))}">{_fmt_time(known_at)}</td>'
            "</tr>"
        )
    body = "".join(rows) if rows else '<tr><td colspan="6">当前无开启中的事件</td></tr>'
    return (
        '<section class="card section"><div class="section-head"><div><h2>需要关注的市场事件</h2>'
        '<p class="subtitle">只读展示客观触发事实；处理状态以 API 为准。</p></div></div>'
        '<div class="table-wrap"><table><thead><tr><th>事件类型</th><th>级别</th><th>市场</th><th>股票代码</th>'
        '<th>事件标识</th><th>事实时间</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div></section>"
    )


def _snap_view(r, security: dict[str, str] | None = None) -> SimpleNamespace:
    """快照行抽平为轻量对象：渲染在 uow session 外进行，
    直接传 ORM 行会因 commit expire 触发 DetachedInstanceError。
    security 来自 security_names 批量查询（§38-39），提供 name/market。"""
    return SimpleNamespace(
        security_id=getattr(r, "security_id", None),
        code=r.code,
        stage=r.stage,
        alive=r.alive,
        score=r.score,
        rank=r.rank,
        drop_reason=r.drop_reason,
        name=(security or {}).get("name"),
        market=(security or {}).get("market"),
    )


def _market_label(market: Any) -> str:
    """§60 市场中文化：SH→沪市 / SZ→深市 / BJ→北交所，未知原样。"""
    if not market:
        return "—"
    return MARKET_LABELS.get(str(market), str(market))


def _stage_label(stage: Any) -> str:
    """阶段中文（title 保留原始 stage 代码）。"""
    label = zh_labels.zh_label(STAGE_LABELS, stage)
    if label == zh_labels.UNMAPPED_LABEL:
        label = str(stage or "—")
    return label


def _scan_funnel_section(
    run,
    expert_counts: dict[str, int] | None = None,
    *,
    query_market: str | None = None,
    query_sort: FeatureSortField = FeatureSortField.RETURN_20D,
    query_descending: bool = True,
    query_limit: int = 50,
) -> str:
    """§44 漏斗 + 专家召回计数 + 任意股票"为什么没入选"搜索表单。"""
    if run is None:
        return ""
    stages = (
        ("UNIVERSE", run.universe_count),
        ("SAFETY", run.eligible_count),
        ("RECALL", run.recall_count),
        ("PARETO", run.pareto_count),
        ("MACHINE", run.machine_count),
        ("DEEP", run.deep_count),
        ("FINAL", run.final_count),
    )
    chips = " ".join(
        f'<span class="badge{" mute" if count == 0 else ""}" title="{escape(stage)}">'
        f"{escape(_stage_label(stage))} {count:,}</span>"
        for stage, count in stages
    )
    expert_note = ""
    if expert_counts:
        expert_note = (
            '<p class="subtitle" style="margin-top:8px">专家命中：'
            + " · ".join(
                f"{escape(EXPERT_LABELS.get(expert, str(expert)))} {count:,}"
                for expert in EXPERT_DISPLAY_ORDER
                if (count := expert_counts.get(expert)) is not None
            )
            + "</p>"
        )
    hidden = (
        f'<input type="hidden" name="market" value="{escape(query_market or "")}">'
        f'<input type="hidden" name="sort_by" value="{escape(query_sort.value)}">'
        f'<input type="hidden" name="descending" value="{str(query_descending).lower()}">'
        f'<input type="hidden" name="limit" value="{query_limit}">'
    )
    return (
        '<section class="card section"><div class="section-head"><div><h2>候选扫描漏斗</h2>'
        f'<p class="subtitle">最新扫描 {escape(str(run.market_date)[:10])} · '
        f'<a href="/api/v3/scan/latest">原始数据（JSON）</a> · 耗时 {run.duration_ms:,} 毫秒</p></div></div>'
        f"<p>{chips}</p>{expert_note}"
        '<form class="controls" method="get" action="/v3/dashboard" style="margin-top:12px">'
        f'{hidden}<input name="trace" type="text" placeholder="输入股票代码，如 600030" '
        'aria-label="股票代码" style="min-width:220px">'
        '<button type="submit">查询为什么没入选</button></form>'
        "</section>"
    )


def _scan_top_section(rows: list[Any], stage: str) -> str:
    """§45/§55 最终候选 Top30：名次|股票代码|股票名称|机器评分|查看筛选轨迹。
    名称来自 SecurityModel 批量查询（禁 N+1），缺失显示"名称暂缺"。"""
    if not rows:
        return ""
    body = "".join(
        "<tr>"
        f'<td class="num">{escape(str(row.rank))}</td>'
        f"<td>{escape(row.code)}</td>"
        f'<td class="name">{escape(row.name or NAME_MISSING_LABEL)}</td>'
        f'<td class="num">{_number(row.score)}</td>'
        f'<td><a href="/v3/dashboard?trace={escape(row.code)}">查看筛选轨迹</a></td>'
        "</tr>"
        for row in rows
    )
    return (
        f'<section class="card section"><div class="section-head"><div><h2>最终候选 Top30</h2>'
        '<p class="subtitle">分数用于候选之间排序，不代表预测涨幅，也不等于买入建议。</p></div></div>'
        '<div class="table-wrap"><table><thead><tr><th class="num">名次</th><th>股票代码</th><th>股票名称</th>'
        '<th class="num">机器评分</th><th>筛选过程</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div></section>"
    )


def _why_not_section(rows: list[Any], code: str, name: str | None = None) -> str:
    """§42-§47 筛选轨迹：任意代码全链生命轨迹，列=阶段|状态|本层分数|
    本层排名|说明；通过行用 STAGE_PASS_NOTES，淘汰行用 DROP_REASON_LABELS，
    support_not_broken 是加分证据按正向说明展示（§52）。"""
    if not rows:
        return (
            '<section class="card section"><h2>筛选轨迹</h2>'
            f"<p class=\"muted\">最新一轮扫描中未找到 {escape(code)} 的记录：该股票可能未进入本轮全市场扫描范围，或代码输入有误。</p></section>"
        )
    final_alive = any(
        str(row.stage) == "FINAL" and row.alive for row in rows
    )
    display_name = name or NAME_MISSING_LABEL
    body = ""
    for row in rows:
        note_title = escape(str(row.drop_reason or row.stage))
        if row.alive:
            # FINAL 存活行展示"已入选"，其余层通过为"通过"
            if str(row.stage) == "FINAL":
                status_html = '<td><span class="badge">已入选</span></td>'
            else:
                status_html = '<td><span class="badge">通过</span></td>'
            # §52：support_not_broken 是加分证据不是淘汰理由，按正向说明展示
            if str(row.drop_reason) == "support_not_broken":
                note = SUPPORT_NOTE_LABEL
            else:
                note = STAGE_PASS_NOTES.get(str(row.stage), "—")
        else:
            status_html = '<td><span class="badge bad">已淘汰</span></td>'
            if row.drop_reason:
                note = DROP_REASON_LABELS.get(str(row.drop_reason), str(row.drop_reason))
            else:
                note = "—"
        body += (
            "<tr>"
            f'<td title="{escape(str(row.stage))}">{escape(_stage_label(row.stage))}</td>'
            f"{status_html}"
            f'<td class="num">{_number(row.score)}</td>'
            f'<td class="num">{_text(row.rank)}</td>'
            f'<td class="missing" title="{note_title}">{escape(note)}</td>'
            "</tr>"
        )
    final_note = (
        '<p class="subtitle">最终结果：该股票<b>已进入最终候选 Top30</b>。</p>'
        if final_alive
        else '<p class="subtitle">最终结果：该股票<b>未进入最终候选 Top30</b>。下方说明列出它在每一层的状态与原因。</p>'
    )
    return (
        f'<section class="card section"><div class="section-head"><div><h2>筛选轨迹 · {escape(display_name)}（{escape(code)}）</h2>'
        f"{final_note}</div></div>"
        '<div class="table-wrap"><table><thead><tr><th>阶段</th><th>状态</th>'
        '<th class="num">本层分数</th><th class="num">本层排名</th><th>说明</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div></section>"
    )


async def _load_feature_table_data(
    uow,
    *,
    market: str | None,
    sort_by: FeatureSortField,
    descending: bool,
    limit: int,
):
    """§66 共享查询：整页与 features-fragment 端点共用，保证两侧
    看到的行情表数据口径一致（筛选参数完全透传）。"""
    return await uow.features.query(
        FeatureQuery(
            market=market,
            sort_by=sort_by,
            descending=descending,
            fields=FEATURE_FIELDS,
            limit=limit,
        )
    )


def _feature_table_nodes(
    page, *, sort_by: FeatureSortField, descending: bool
) -> tuple[str, str]:
    """§67/§75 共享渲染：返回 (summary_html, result_html) 两个节点——
    <span id="feature-table-summary"> 与 <div id="feature-table-result">。
    整页渲染与 fragment 端点共用，局部刷新后 DOM 与整页一致；
    fragment 响应只由这两个节点组成（不含 doctype）。"""
    rows = []
    for rank, item in enumerate(page.items, 1):
        # §35 缺失字段中文名（title 保留原始字段名），未映射兜底原样
        missing_raw = item.get("missing_fields") or []
        missing = ", ".join(
            FIELD_LABELS.get(str(field), str(field)) for field in missing_raw
        )
        missing_title = ", ".join(str(field) for field in missing_raw)
        rows.append(
            "<tr>"
            f'<td class="num">{rank}</td><td>{escape(_market_label(item.get("market")))}</td>'
            f'<td>{_text(item.get("code"))}</td><td class="name">{_text(item.get("name"))}</td>'
            f'<td class="num">{_number(item.get("close"))}</td>'
            f'{_return_cell(item.get("return_3d"))}{_return_cell(item.get("return_5d"))}'
            f'{_return_cell(item.get("return_20d"))}{_return_cell(item.get("return_60d"))}'
            f'<td class="num">{_pct(item.get("position_60d"), fraction=True)}</td>'
            f'<td class="num">{_number(item.get("volume_ratio_5d"))}</td>'
            f'<td class="num">{_pct(item.get("atr_pct"), fraction=True)}</td>'
            f'<td class="num" title="{_text(item.get("amount"))}">{_amount(item.get("amount"))}</td>'
            f'<td class="num">{_pct(item.get("coverage"), fraction=True)}</td>'
            f'<td><span class="badge{" warn" if item.get("stale") else ""}">{"数据过期" if item.get("stale") else "数据新鲜"}</span></td>'
            f'<td class="missing" title="{_text(missing_title)}">{_text(missing)}</td>'
            "</tr>"
        )
    summary = (
        f"筛选后可查询 {page.total_count:,} 条；当前按“{escape(SORT_LABELS[sort_by])}”"
        f"{'降序' if descending else '升序'}展示，最多读取 100 条。"
    )
    summary_html = (
        f'<span id="feature-table-summary" class="subtitle" '
        f'style="display:block;margin-top:4px">{summary}</span>'
    )
    result_html = (
        '<div class="table-wrap" id="feature-table-result">'
        '<table><thead><tr><th class="num">#</th><th>市场</th><th>代码</th><th>名称</th>'
        '<th class="num">收盘价</th><th class="num">3日涨跌</th><th class="num">5日涨跌</th>'
        '<th class="num">20日涨跌</th><th class="num">60日涨跌</th><th class="num">60日价格位置</th>'
        '<th class="num">5日量比</th><th class="num">ATR波动率</th><th class="num">成交额</th>'
        '<th class="num">数据覆盖率</th><th>数据状态</th><th>缺失字段</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    return summary_html, result_html


_FEATURE_FILTER_SCRIPT = """
(function () {
  var form = document.getElementById('feature-filter-form');
  if (!form || !window.fetch || !window.AbortController) { return; }
  var errorEl = document.getElementById('feature-filter-error');
  var controller = null;
  form.addEventListener('submit', function (event) {
    // §77 渐进增强：JS 可用时拦截 GET 提交改局部刷新；否则原生提交兜底
    event.preventDefault();
    if (controller) { controller.abort(); }  // §73 防重复提交
    controller = new AbortController();
    var params = new URLSearchParams(new FormData(form));
    var button = form.querySelector('button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = '加载中…'; }  // §71
    errorEl.hidden = true;
    var scrollY = window.scrollY;
    fetch('/v3/dashboard/features-fragment?' + params.toString(), {
      headers: { 'X-Requested-With': 'fetch' },
      cache: 'no-store',
      signal: controller.signal
    }).then(function (resp) {
      if (!resp.ok) { throw new Error('HTTP ' + resp.status); }
      return resp.text();
    }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      ['feature-table-summary', 'feature-table-result'].forEach(function (id) {
        var remote = doc.getElementById(id);
        var local = document.getElementById(id);
        if (remote && local) { local.outerHTML = remote.outerHTML; }
      });
      // §70 保留 URL 可分享/可回退，不产生历史记录
      history.replaceState(null, '', '/v3/dashboard?' + params.toString());
      window.scrollTo(0, scrollY);
    }).catch(function (err) {
      if (err && err.name === 'AbortError') { return; }
      // §72 失败保留原表格，仅提示
      errorEl.textContent = '加载失败，请稍后重试';
      errorEl.hidden = false;
    }).then(function () {
      if (button) { button.disabled = false; button.textContent = '应用'; }
      controller = null;
    });
  });
})();
"""


def render_dashboard(page, regime, *, sort_by: FeatureSortField, descending: bool, market: str | None, limit: int,
                     intraday_status: dict[str, Any] | None = None,
                     pipeline: dict[str, Any] | None = None,
                     attention_events: list[Any] | None = None,
                     scan_run=None,
                     scan_expert_counts: dict[str, int] | None = None,
                     scan_top_rows: list[Any] | None = None,
                     why_not_rows: list[Any] | None = None,
                     why_not_code: str | None = None,
                     why_not_name: str | None = None) -> str:
    quality = page.quality_summary
    coverage = float(quality.get("coverage", 0))
    successful = int(quality.get("successful_count", 0))
    failed = int(quality.get("failed_count", 0))
    expected = successful + failed
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
    summary_html, result_html = _feature_table_nodes(
        page, sort_by=sort_by, descending=descending,
    )
    regime_html = ""
    if regime is not None:
        stale_reason = getattr(regime, "stale_reason", None) or {}
        cause = stale_reason.get("cause")
        stale_badge = (
            '<span class="badge bad">市场状态已过期</span>'
            if regime.stale
            else '<span class="badge">市场状态新鲜</span>'
        )
        cause_note = (
            f' · 陈旧行 {escape(str(stale_reason.get("stale_count")))}'
            f'/{escape(str(stale_reason.get("total_count")))}'
            f'（阈值 {escape(str(stale_reason.get("threshold")))}, {escape(str(cause))}）'
            if stale_reason
            else ""
        )
        regime_html = (
            '<section class="card section"><div class="section-head"><div><h2>市场整体状态</h2>'
            '<p class="subtitle">根据全市场涨跌、成交、突破与风险偏好事实汇总。</p>'
            f"<p class=\"subtitle\">{stale_badge}{cause_note}</p></div></div>"
            '<div class="facts">'
            + _fact_rows(REGIME_SECTION_LABELS["breadth"], regime.breadth, "breadth")
            + _fact_rows(REGIME_SECTION_LABELS["turnover"], regime.turnover, "turnover")
            + _fact_rows(REGIME_SECTION_LABELS["risk_appetite"], regime.risk_appetite_facts, "risk_appetite")
            + "</div></section>"
        )
    trace_query = why_not_code and f"trace={escape(why_not_code)}" or ""
    body = f"""
<section class="card hero"><div><span class="badge">V3 只读</span><h1 style="margin-top:8px">V3 全市场行情特征看板</h1>
<p class="subtitle">展示不可变 Feature Run 的事实特征；当前排序不是统一评分，也不构成投资建议。</p></div>
<div class="meta">数据时间：<span title="{escape(page.as_of.isoformat())}">{_fmt_time(page.as_of)}</span><br>特征版本：{_text(page.feature_version)}<br>运行编号：{_text(page.feature_run_id)}</div></section>
<div class="stats"><section class="card stat"><span>本轮证券总数</span><strong>{expected:,}</strong></section>
<section class="card stat"><span>成功</span><strong>{successful:,}</strong></section><section class="card stat"><span>失败</span><strong>{failed:,}</strong></section>
<section class="card stat"><span>覆盖率</span><strong>{coverage * 100:.2f}%</strong></section><section class="card stat"><span>当前页数据过期</span><strong>{stale_count}</strong></section></div>
{regime_html}
{_scan_funnel_section(scan_run, scan_expert_counts, query_market=market, query_sort=sort_by, query_descending=descending, query_limit=limit)}
{_scan_top_section(scan_top_rows or [], 'FINAL')}
{f'{_why_not_section(why_not_rows or [], why_not_code or "", why_not_name)}' if trace_query else ''}
{_live_status_section(intraday_status)}
{_pipeline_section(pipeline)}
{_attention_section(attention_events or [])}
<section class="card section" id="feature-table-section"><div class="section-head"><div><h2>全市场事实特征{summary_html}</h2></div>
<form class="controls" method="get" action="/v3/dashboard" id="feature-filter-form"><select name="market">{market_options}</select><select name="sort_by">{sort_options}</select>
<select name="descending">{direction_options}</select><input name="limit" type="number" min="20" max="100" value="{limit}" aria-label="显示数量"><button type="submit">应用</button></form></div>
{result_html}
<p id="feature-filter-error" class="muted" hidden></p></section>
<script>{_FEATURE_FILTER_SCRIPT}</script>
<footer class="foot">服务器时间：{escape(now_shanghai().strftime("%Y-%m-%d %H:%M:%S"))}（上海） · <a href="/api/v3/universe/features">原始数据（JSON）</a> · <a href="/docs">API 文档</a></footer>
"""
    return _document("V3 全市场行情特征看板", body)


@router.get("/dashboard", response_class=HTMLResponse)
async def v3_dashboard(
    market: str | None = Query(default=None),
    sort_by: FeatureSortField = FeatureSortField.RETURN_20D,
    descending: bool = True,
    limit: str | None = Query(default=None),
    trace: str | None = Query(default=None, description="查看任意股票的筛选轨迹"),
    whynot: str | None = Query(default=None, description="Deprecated：等价于 trace"),
):
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    # §43 新参数 trace=；whynot= 保留兼容（旧链接不失效）
    trace_code = trace or whynot
    # 表单 GET 提交时“全部”市场/空条数会带空串，必须当作缺省而不是 422
    if market == "":
        market = None
    if market is not None and market not in {"SH", "SZ", "BJ"}:
        raise HTTPException(status_code=422, detail="market must be one of SH/SZ/BJ")
    try:
        limit_value = 50 if limit in (None, "") else int(limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="limit must be an integer") from exc
    if not 20 <= limit_value <= 100:
        raise HTTPException(status_code=422, detail="limit must be between 20 and 100")
    calendar = ExchangeCalendarsAShareCalendar()

    def _trading_day(value):
        try:
            return bool(calendar.is_trading_day(value))
        except Exception:
            return False

    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    async with container.v3.uow() as uow:
        page = await _load_feature_table_data(
            uow, market=market, sort_by=sort_by,
            descending=descending, limit=limit_value,
        )
        regime = await uow.features.latest_regime()
        attention_events = await uow.attention.open_events(limit=20)
        # §37 候选扫描区块（无扫描数据/UoW 无 scans 时各 section 自动缺席）
        # scan_run 抽成 SimpleNamespace：ORM 行在 uow commit 后 expire，
        # 渲染发生在 session 外，直接传 model 会触发 DetachedInstanceError
        scans = getattr(uow, "scans", None)
        scan_run_model = await scans.latest_run() if scans is not None else None
        scan_run = None
        scan_expert_counts = scan_top_rows = why_not_rows = None
        trace_name = None
        if scans is not None and scan_run_model is not None:
            scan_run = SimpleNamespace(
                scan_run_id=scan_run_model.scan_run_id,
                scan_time=scan_run_model.scan_time,
                market_date=scan_run_model.market_date,
                universe_count=scan_run_model.universe_count,
                eligible_count=scan_run_model.eligible_count,
                recall_count=scan_run_model.recall_count,
                pareto_count=scan_run_model.pareto_count,
                machine_count=scan_run_model.machine_count,
                deep_count=scan_run_model.deep_count,
                final_count=scan_run_model.final_count,
                duration_ms=scan_run_model.duration_ms,
            )
            expert_rows = await uow.scans.expert_rows(scan_run.scan_run_id)
            counts: dict[str, int] = {}
            for row in expert_rows:
                counts[row.expert] = counts.get(row.expert, 0) + 1
            scan_expert_counts = counts
            top_models = await uow.scans.snapshots(
                scan_run.scan_run_id, stage="FINAL", alive_only=True, limit=30,
            )
            trace_models = (
                await uow.scans.snapshots(
                    scan_run.scan_run_id, code=trace_code, limit=64,
                )
                if trace_code
                else []
            )
            # §38-39 名称批量查询：Top30 + 轨迹行合并做一次 IN 查询，禁止逐行 N+1
            ids = [
                m.security_id
                for m in (*top_models, *trace_models)
                if getattr(m, "security_id", None) is not None
            ]
            names = await uow.scans.security_names(ids) if ids else {}
            scan_top_rows = [_snap_view(m, names.get(m.security_id)) for m in top_models]
            if trace_code:
                why_not_rows = [
                    _snap_view(m, names.get(m.security_id)) for m in trace_models
                ]
                if trace_models:
                    first_id = trace_models[0].security_id
                    trace_name = (names.get(first_id) or {}).get("name")
    intraday_status = await MarketIntradayStatusService(
        clock=clock, is_trading_day=_trading_day,
    ).execute()
    pipeline = await PipelineEodLatestService(
        container.v3.uow, clock=clock,
    ).execute()
    if page is None:
        return initializing_page("生产库尚未发布 Feature Run；后台数据任务完成后即可展示。")
    return HTMLResponse(
        render_dashboard(
            page,
            regime,
            sort_by=sort_by,
            descending=descending,
            market=market,
            limit=limit_value,
            intraday_status=intraday_status,
            pipeline=pipeline,
            attention_events=attention_events,
            scan_run=scan_run,
            scan_expert_counts=scan_expert_counts,
            scan_top_rows=scan_top_rows,
            why_not_rows=why_not_rows,
            why_not_code=trace_code,
            why_not_name=trace_name,
        ),
        headers=NO_CACHE_HEADERS,
    )


@router.get("/dashboard/features-fragment", response_class=HTMLResponse)
async def v3_dashboard_features_fragment(
    market: str | None = Query(default=None),
    sort_by: FeatureSortField = FeatureSortField.RETURN_20D,
    descending: bool = True,
    limit: str | None = Query(default=None),
):
    """§61-§75：行情表局部刷新 fragment（供 vanilla JS fetch 替换）。

    只返回 #feature-table-summary 与 #feature-table-result 两个节点的
    HTML（不含 doctype/<html>），与整页共享 _load_feature_table_data
    查询与 _feature_table_nodes 渲染，保证局部刷新后 DOM 一致。
    """
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    if market == "":
        market = None
    if market is not None and market not in {"SH", "SZ", "BJ"}:
        raise HTTPException(status_code=422, detail="market must be one of SH/SZ/BJ")
    try:
        limit_value = 50 if limit in (None, "") else int(limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="limit must be an integer") from exc
    if not 20 <= limit_value <= 100:
        raise HTTPException(status_code=422, detail="limit must be between 20 and 100")
    async with container.v3.uow() as uow:
        page = await _load_feature_table_data(
            uow, market=market, sort_by=sort_by,
            descending=descending, limit=limit_value,
        )
    if page is None:
        return HTMLResponse(
            '<span id="feature-table-summary" class="subtitle">暂无数据</span>'
            '<div class="table-wrap" id="feature-table-result">'
            '<p class="muted">暂无可展示的特征数据；后台数据任务完成后即可展示。</p></div>',
            headers=NO_CACHE_HEADERS,
        )
    summary_html, result_html = _feature_table_nodes(
        page, sort_by=sort_by, descending=descending,
    )
    return HTMLResponse(summary_html + result_html, headers=NO_CACHE_HEADERS)
