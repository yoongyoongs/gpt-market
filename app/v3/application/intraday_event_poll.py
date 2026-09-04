"""R5-P1-008/§31/§66：盘中事件轮询（Evidence + Structure Change）。

AttentionEngine.record_new_evidence 此前只有测试调用——盘中
"Evidence 增量 → materiality → 池内 → NEW_EVIDENCE → NEED_AI_REVIEW"
链没有生产调用方。本服务补上 Resident Runtime 轮询：

- 池 = Portfolio 现仓 + Watchlist 现态（与 R5-02/R5-03 同一定义）；
- Evidence：对池内每只券读 Evidence Read（subject 精确匹配），
  known_at 落在窗口内的记录 → materiality（以 relevance 为代理，
  §6.3 无独立字段）≥ 阈值 → engine.record_new_evidence；
- Structure：Deep Market Data（5m/15m/60m 趋势）与上次轮询快照比对，
  趋势翻转 → engine.record_structure_changes（快照为进程内存态，
  事件去抖由 engine cooldown 负责）；
- 安全边界：AttentionEvent != Trade——本服务只产生事件，绝不改
  Decision、绝不产生 Trade。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.v3.domain.evidence import EvidenceReadQuery

# 与 R5-02/R5-03 一致：进入 Active Pool 的 Watchlist 现态
_ACTIVE_WATCHLIST_STATES = {"WATCHING", "WAIT_ENTRY", "ACTION_READY"}
_WATCHLIST_LIMIT = 5000
_PORTFOLIO_LIMIT = 200
_EVIDENCE_PER_SECURITY = 20
# 结构扫描上限：Deep 分钟级抓取按券 3 次调用，池大时须有界
_STRUCTURE_SCAN_LIMIT = 30


class _WatchlistRepo(Protocol):
    async def read_watchlist(self, state: str | None, limit: int) -> Any: ...


class _ReadsRepo(Protocol):
    async def portfolio_overview(self, limit: int) -> dict: ...


class _EvidenceRepo(Protocol):
    async def retrieve_view(self, *, query: Any) -> Any: ...


class _Uow(Protocol):
    ai_imports: _WatchlistRepo
    reads: _ReadsRepo
    evidence: _EvidenceRepo

    async def __aenter__(self) -> "_Uow": ...

    async def __aexit__(self, *args) -> None: ...


class _EventEngine(Protocol):
    async def record_new_evidence(self, items: Any, *, universe: Any,
                                  as_of: datetime) -> Any: ...

    async def record_structure_changes(self, changes: Any, *,
                                       as_of: datetime) -> Any: ...


class _DeepService(Protocol):
    async def get_intraday_structure(self, code: str, *,
                                     as_of: datetime) -> Any: ...


class IntradayEventPollService:
    def __init__(
        self,
        uow_factory: Callable[[], _Uow],
        engine: _EventEngine,
        deep_service: _DeepService | None = None,
        *,
        evidence_window_seconds: float = 1800.0,
        structure_scan_limit: int = _STRUCTURE_SCAN_LIMIT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._engine = engine
        self._deep = deep_service
        self._window = timedelta(seconds=evidence_window_seconds)
        self._structure_scan_limit = structure_scan_limit
        # (market, code, timeframe) -> trend：进程内存快照（去抖靠 engine）
        self._last_trends: dict[tuple[str, str, str], str] = {}

    async def execute(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        as_of = as_of or datetime.now().astimezone()
        since = as_of - self._window
        pool, security_ids = await self._load_pool()
        universe = set(pool)
        report: dict[str, Any] = {
            "source": "intraday-event-poll-v1",
            "as_of": as_of.isoformat(),
            "pool_size": len(pool),
        }
        evidence_eval = await self._poll_evidence(
            pool, security_ids, since=since, universe=universe, as_of=as_of,
        )
        report["evidence"] = {
            "items": evidence_eval.get("items"),
            "created": evidence_eval.get("created"),
            "skipped": evidence_eval.get("skipped"),
        }
        structure_eval = await self._poll_structure(pool, as_of=as_of)
        report["structure"] = {
            "scanned": structure_eval.get("scanned"),
            "changes": structure_eval.get("changes"),
            "created": structure_eval.get("created"),
        }
        report["status"] = "AVAILABLE"
        return report

    async def _load_pool(
        self,
    ) -> tuple[set[tuple[str, str]], dict[tuple[str, str], Any]]:
        pool: set[tuple[str, str]] = set()
        security_ids: dict[tuple[str, str], Any] = {}
        async with self._uow_factory() as uow:
            for row in await uow.ai_imports.read_watchlist(
                None, _WATCHLIST_LIMIT,
            ):
                if row.get("state") not in _ACTIVE_WATCHLIST_STATES:
                    continue
                key = (row.get("security_market"), row.get("security_code"))
                if key[0] and key[1]:
                    pool.add(key)
                    security_ids[key] = row.get("security_id")
            overview = await uow.reads.portfolio_overview(
                limit=_PORTFOLIO_LIMIT,
            )
            for account in overview.get("accounts", ()):
                for position in account.get("positions", ()):
                    if (position.get("quantity") or 0) <= 0:
                        continue
                    key = (
                        position.get("security_market"),
                        position.get("security_code"),
                    )
                    if key[0] and key[1]:
                        pool.add(key)
                        security_ids.setdefault(key, position.get("security_id"))
        return pool, security_ids

    async def _poll_evidence(
        self,
        pool: set[tuple[str, str]],
        security_ids: dict[tuple[str, str], Any],
        *,
        since: datetime,
        universe: set[tuple[str, str]],
        as_of: datetime,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        failed = 0
        async with self._uow_factory() as uow:
            for market, code in sorted(pool):
                try:
                    page = await uow.evidence.retrieve_view(query=EvidenceReadQuery(
                        subject_type="SECURITY",
                        subject_id=f"{market}:{code}",
                        as_of=as_of,
                        include_candidates=False,
                        limit=_EVIDENCE_PER_SECURITY,
                    ))
                except Exception:  # noqa: BLE001 - 单券失败不阻断轮询
                    failed += 1
                    continue
                for view in getattr(page, "views", ()) or ():
                    record = view.record
                    known_at = getattr(record, "known_at", None)
                    if known_at is None or known_at < since:
                        continue  # 窗口外的"旧"证据不算增量
                    payload = getattr(record, "normalized_payload", None) or {}
                    items.append({
                        "evidence_id": str(record.evidence_id),
                        "market": market, "code": code,
                        # §6.3 无独立 materiality 字段：以 relevance 为代理
                        "materiality": float(record.relevance),
                        "title": payload.get("title"),
                        "evidence_type": str(record.evidence_type),
                        "known_at": known_at.isoformat(),
                    })
        if items:
            evaluation = await self._engine.record_new_evidence(
                items, universe=universe, as_of=as_of,
            )
            created = len(getattr(evaluation, "created", ()) or ())
            skipped = getattr(evaluation, "skipped", 0) or 0
        else:
            created = skipped = 0
        return {"items": len(items), "created": created,
                "skipped": skipped, "failed": failed}

    async def _poll_structure(
        self, pool: set[tuple[str, str]], *, as_of: datetime,
    ) -> dict[str, Any]:
        if self._deep is None:
            return {"scanned": 0, "changes": [], "created": 0,
                    "status": "NOT_WIRED"}
        changes: list[dict[str, Any]] = []
        scanned = 0
        for market, code in sorted(pool)[: self._structure_scan_limit]:
            try:
                structure = await self._deep.get_intraday_structure(
                    code, as_of=as_of,
                )
            except Exception:  # noqa: BLE001 - 单券 Deep 失败不阻断
                continue
            scanned += 1
            periods = getattr(structure, "periods", None) or {}
            for timeframe, detail in periods.items():
                if not isinstance(detail, dict):
                    continue
                inner = detail.get("structure") or {}
                trend = inner.get("trend")
                if trend in (None, "UNKNOWN"):
                    continue
                key = (market, code, timeframe)
                previous = self._last_trends.get(key)
                self._last_trends[key] = trend
                if previous is not None and previous != trend:
                    changes.append({
                        "market": market, "code": code,
                        "timeframe": timeframe,
                        "from_trend": previous, "to_trend": trend,
                        "structure": {
                            "support": inner.get("support"),
                            "resistance": inner.get("resistance"),
                        },
                    })
        if changes:
            evaluation = await self._engine.record_structure_changes(
                changes, as_of=as_of,
            )
            created = len(getattr(evaluation, "created", ()) or ())
        else:
            created = 0
        return {"scanned": scanned, "changes": changes, "created": created}
