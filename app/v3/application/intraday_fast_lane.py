"""R4-P1-003：Intraday Fast Lane 生产接线（实时方案 §5 / 复验 §28）。

盘中链路：全市场 Quote → L1 Overlay → Lightweight Scanner →
IntradayAttentionCandidate → Active Intraday Universe → 重点池
DeepMarketData → Attention。

- 数据源全部来自既有 UoW 仓储与 ProviderManager（get_all_a_shares
  批量快照 + get_index_quote 相对指数），不新造数据通道；
- Overlay 只用最近一次 Published EOD Feature 轻量叠加（FeatureQuery
  上限 200/页，此处按 cursor 翻页，总量受 feature_limit 约束）；
- Scanner stale Quote 绝不入选；Deep 只抓前 deep_limit 只重点候选
  （fetch-time 事实，不落库）；
- Attention：scanner 异常 → INTRADAY_ANOMALY；stale Quote ∩ 池内 →
  DATA_QUALITY_DEGRADED（engine 统一 Gate，R4-P1-002）；
- emit_attention=False 时只读（MCP scan 用），绝不写 AttentionEvent。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Protocol

from app.v3.application.intraday_market_data import map_quote_snapshot
from app.v3.domain.features import FeatureQuery, FeatureSortField

_SOURCE = "intraday-fast-lane-v1"
_FEATURE_FIELDS = (
    "code", "market", "name", "close", "ma5", "ma10", "ma20", "ma60",
    "return_20d", "coverage", "stale",
)
_INDEX_CODE = "000001"  # 上证指数：相对强度基准
_PAGE_LIMIT = 200  # FeatureQuery.limit 合同上限
# R5-P1-004/§62：全市场 Coverage Gate——部分市场绝不冒充全市场扫描。
# coverage >= 0.9 → AVAILABLE；0.5 ~ 0.9 → PARTIAL；< 0.5 →
# UNAVAILABLE_FOR_FULL_MARKET_SCAN。
_FULL_MARKET_PARTIAL_RATIO = 0.9
_FULL_MARKET_UNAVAILABLE_RATIO = 0.5


class _FeaturesRepo(Protocol):
    async def query(self, query: FeatureQuery) -> Any: ...


class _RecallsRepo(Protocol):
    async def read_results(self, **kwargs) -> Any: ...


class _ReadsRepo(Protocol):
    async def watchlist_changes(self, limit: int) -> list[dict]: ...
    async def portfolio_overview(self, limit: int) -> dict: ...


class _Uow(Protocol):
    features: _FeaturesRepo
    recalls: _RecallsRepo
    reads: _ReadsRepo

    async def __aenter__(self) -> "_Uow": ...
    async def __aexit__(self, *args) -> None: ...


class _QuoteProvider(Protocol):
    async def get_all_a_shares(self) -> tuple[int, list[Any]]: ...
    async def get_index_quote(self, code: str, market: str) -> Any: ...


class _Engine(Protocol):
    async def record_intraday_anomalies(self, candidates: Any, *, as_of: datetime) -> Any: ...
    async def record_data_quality(self, quotes: Any, *, universe: Any, as_of: datetime) -> Any: ...


def _index_return(snapshot: Any) -> float | None:
    price = getattr(snapshot, "last_price", None)
    prev = getattr(snapshot, "prev_close", None)
    if price is None or not prev:
        return None
    return price / prev - 1


# R5-P1-002/§61：Deep 优先级冻结——Portfolio > Watchlist（当前有效态）
# > EOD Candidate > Intraday Candidate。条目带多来源时取最高优先级。
_DEEP_PRIORITY = ("PORTFOLIO", "WATCHLIST", "EOD_CANDIDATE", "INTRADAY_ATTENTION")


def _deep_priority(entry: Any) -> int:
    ranks = [
        _DEEP_PRIORITY.index(source)
        for source in getattr(entry, "sources", ())
        if source in _DEEP_PRIORITY
    ]
    return min(ranks) if ranks else len(_DEEP_PRIORITY)


class IntradayFastLaneService:
    def __init__(
        self,
        uow_factory: Any,
        quote_provider: _QuoteProvider,
        overlay_service: Any,
        scanner: Any,
        pool_service: Any,
        *,
        engine: _Engine | None = None,
        deep_service: Any = None,
        feature_limit: int = 2000,
        deep_limit: int = 10,
        clock: Any = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._quote_provider = quote_provider
        self._overlay = overlay_service
        self._scanner = scanner
        self._pool_service = pool_service
        self._engine = engine
        self._deep = deep_service
        self._feature_limit = feature_limit
        self._deep_limit = deep_limit
        self._clock = clock or (lambda: datetime.now().astimezone())

    async def execute(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        as_of = as_of or self._clock()
        report: dict[str, Any] = {"source": _SOURCE, "as_of": as_of}
        features, eod, watchlist, portfolio, sources = await self._load_state()
        report["sources"] = sources
        if sources["features"]["status"] != "AVAILABLE":
            report["features_error"] = sources["features"].get("error")
            report["overlay_status"] = "DEGRADED"

        # 全市场 Quote（东财 clist 批量）→ V3 快照（含 R5-01 时间语义）
        try:
            expected_count, raw_quotes = (
                await self._quote_provider.get_all_a_shares()
            )
        except Exception as exc:  # noqa: BLE001 - 行情失败如实上报，绝不伪造
            report.update({
                "status": "QUOTE_FAILED",
                "quote_error": f"{type(exc).__name__}: {exc}",
                "quote_expected": 0, "quote_actual": 0,
                "quote_missing": 0, "quote_coverage": None,
                "candidate_count": 0,
                "pool_size": len(eod | watchlist | portfolio),
            })
            return report
        snapshots = [map_quote_snapshot(quote, as_of) for quote in raw_quotes]
        # R5-P1-004/§62：Coverage Gate——丢弃 total、静默丢行后宣称
        # 全市场扫描完成是谎言；expected/actual/missing/coverage 全透传。
        expected = int(expected_count or 0)
        quote_count = len(snapshots)
        missing = max(0, expected - quote_count)
        coverage = (quote_count / expected) if expected > 0 else None
        if coverage is None:
            status = "AVAILABLE"  # provider 未报告 expected，无法判定缺失
        elif coverage < _FULL_MARKET_UNAVAILABLE_RATIO:
            status = "UNAVAILABLE_FOR_FULL_MARKET_SCAN"
        elif coverage < _FULL_MARKET_PARTIAL_RATIO:
            status = "PARTIAL"
        else:
            status = "AVAILABLE"
        report.update({
            "status": status,
            "quote_expected": expected,
            "quote_actual": quote_count,
            "quote_count": quote_count,  # 兼容旧消费方（MCP scan）
            "quote_missing": missing,
            "quote_coverage": (
                round(coverage, 4) if coverage is not None else None
            ),
            "full_market_complete": status == "AVAILABLE",
        })
        report["stale_quote_count"] = sum(1 for item in snapshots if item.stale)

        index_return = await self._load_index_return(as_of)
        overlays = {
            snapshot.code: self._overlay.build(
                code=snapshot.code, market=snapshot.market,
                quote=snapshot, feature=features.get(snapshot.code),
                index_return=index_return, as_of=as_of,
            )
            for snapshot in snapshots
        }
        candidates = self._scanner.scan(
            snapshots, overlays, index_return=index_return, as_of=as_of,
        )
        report["candidate_count"] = len(candidates)
        report["candidates"] = [
            {
                "code": item.code, "market": item.market,
                "reasons": list(item.reasons),
                "latest_price": item.latest_price,
                "intraday_return": item.intraday_return,
                "volume_ratio": item.volume_ratio,
            }
            for item in candidates
        ]

        pool = self._pool_service.merge(
            eod_candidates=sorted(eod),
            watchlist=sorted(watchlist),
            portfolio=sorted(portfolio),
            intraday_attention=sorted(
                (item.market, item.code) for item in candidates
            ),
        )
        report["pool_size"] = len(pool)
        universe = {(entry.market, entry.code) for entry in pool}

        # Attention 写入（常驻 Loop 路径）；engine=None 时只读（MCP scan）
        attention = {"anomaly_created": 0, "data_quality_created": 0}
        if self._engine is not None:
            if candidates:
                anomaly = await self._engine.record_intraday_anomalies(
                    candidates, as_of=as_of,
                )
                attention["anomaly_created"] = len(anomaly.created)
            stale_in_pool = [
                item for item in snapshots
                if item.stale and (item.market, item.code) in universe
            ]
            if stale_in_pool:
                degraded = await self._engine.record_data_quality(
                    stale_in_pool, universe=universe, as_of=as_of,
                )
                attention["data_quality_created"] = len(degraded.created)
        report["attention"] = attention

        # R5-P1-002/§61：Deep 输入必须是 merged Active Pool（不是 Scanner
        # 候选）——Scanner 为空时 Portfolio/Watchlist/EOD 仍获深度刷新；
        # 受 deep_limit 时按冻结优先级裁剪（fetch-time 事实，不落库）。
        ranked_pool = sorted(pool, key=_deep_priority)
        report["deep"] = await self._deep_summaries(ranked_pool, as_of)
        return report

    async def _load_state(
        self,
    ) -> tuple[dict[str, Any], set, set, set, dict[str, dict[str, Any]]]:
        """一次 UoW 会话读全：特征翻页 + EOD 候选 + Watchlist + 持仓。

        R5-P1-003/§62.4：四个来源独立 try——单源失败只标记该源 FAILED
        并 rollback 会话，绝不把其它来源一起清空（"特征失败不阻断
        Recall/Watchlist/Portfolio"必须有实现支撑）。会话本身建不起来
        → 四源全部 FAILED，如实上报。
        """
        features: dict[str, Any] = {}
        eod: set[tuple[str, str]] = set()
        watchlist: set[tuple[str, str]] = set()
        portfolio: set[tuple[str, str]] = set()
        sources: dict[str, dict[str, Any]] = {}

        def _ok(name: str, count: int) -> None:
            sources[name] = {"status": "AVAILABLE", "count": count}

        def _fail(name: str, exc: Exception) -> None:
            sources[name] = {
                "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
                "count": 0,
            }

        try:
            async with self._uow_factory() as uow:
                try:
                    cursor: str | None = None
                    for _ in range(max(1, self._feature_limit // _PAGE_LIMIT)):
                        page = await uow.features.query(FeatureQuery(
                            sort_by=FeatureSortField.AMOUNT, descending=True,
                            fields=_FEATURE_FIELDS, limit=_PAGE_LIMIT,
                            cursor=cursor,
                        ))
                        if page is None:
                            break
                        for item in page.items:
                            code = item.get("code")
                            if code:
                                features[code] = SimpleNamespace(
                                    features=item, as_of=page.as_of,
                                )
                        cursor = page.next_cursor
                        if cursor is None or not page.items:
                            break
                    _ok("features", len(features))
                except Exception as exc:  # noqa: BLE001 - Overlay 降级
                    await uow.rollback()
                    _fail("features", exc)
                try:
                    recall_page = await uow.recalls.read_results(
                        recall_run_id=None, channel_code=None,
                        limit=_PAGE_LIMIT, cursor=None,
                    )
                    if recall_page is not None:
                        eod = {
                            (item.market, item.code)
                            for item in recall_page.items
                        }
                    _ok("eod", len(eod))
                except Exception as exc:  # noqa: BLE001 - EOD 源独立降级
                    await uow.rollback()
                    _fail("eod", exc)
                try:
                    for row in await uow.reads.watchlist_changes(limit=500):
                        if row.get("current_state") == "WATCHING" and row.get(
                            "security_market",
                        ) and row.get("security_code"):
                            watchlist.add((
                                row["security_market"], row["security_code"],
                            ))
                    _ok("watchlist", len(watchlist))
                except Exception as exc:  # noqa: BLE001 - Watchlist 独立降级
                    await uow.rollback()
                    _fail("watchlist", exc)
                try:
                    overview = await uow.reads.portfolio_overview(limit=200)
                    for account in overview.get("accounts", ()):
                        for position in account.get("positions", ()):
                            if (position.get("quantity") or 0) > 0:
                                portfolio.add((
                                    position.get("security_market"),
                                    position.get("security_code"),
                                ))
                    _ok("portfolio", len(portfolio))
                except Exception as exc:  # noqa: BLE001 - 持仓独立降级
                    await uow.rollback()
                    _fail("portfolio", exc)
        except Exception as exc:  # noqa: BLE001 - 会话级失败如实全标
            for name in ("features", "eod", "watchlist", "portfolio"):
                sources.setdefault(name, {
                    "status": "FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                    "count": 0,
                })
        portfolio = {
            (market, code) for market, code in portfolio if market and code
        }
        watchlist = {
            (market, code) for market, code in watchlist if market and code
        }
        return features, eod, watchlist, portfolio, sources

    async def _load_index_return(self, as_of: datetime) -> float | None:
        try:
            index_quote = await self._quote_provider.get_index_quote(
                _INDEX_CODE, "SH",
            )
        except Exception:  # noqa: BLE001 - 指数失败只影响相对强度一个指标
            return None
        return _index_return(map_quote_snapshot(index_quote, as_of))

    async def _deep_summaries(
        self, pool: tuple[Any, ...], as_of: datetime,
    ) -> list[dict[str, Any]]:
        if self._deep is None or not pool:
            return []
        summaries: list[dict[str, Any]] = []
        for entry in pool[: self._deep_limit]:
            try:
                structure = await self._deep.get_intraday_structure(
                    entry.code, as_of=as_of,
                )
            except Exception as exc:  # noqa: BLE001 - 单票 Deep 失败隔离
                summaries.append({
                    "code": entry.code, "market": entry.market,
                    "sources": list(getattr(entry, "sources", ())),
                    "status": "UNKNOWN",
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue
            summaries.append({
                "code": entry.code, "market": entry.market,
                "sources": list(getattr(entry, "sources", ())),
                "status": "AVAILABLE",
                "weekly_trend": getattr(
                    getattr(structure, "weekly", None), "trend", None,
                ),
                "daily_trend": getattr(
                    getattr(structure, "daily", None), "trend", None,
                ),
                "reversal_state": getattr(structure, "reversal_state", None),
                "conflict": getattr(structure, "conflict", None),
            })
        return summaries
