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
        features, features_error, eod, watchlist, portfolio = await self._load_state()
        if features_error is not None:
            report["features_error"] = features_error

        # 全市场 Quote（东财 clist 批量）→ V3 快照（含 Future Guard）
        try:
            _, raw_quotes = await self._quote_provider.get_all_a_shares()
        except Exception as exc:  # noqa: BLE001 - 行情失败如实上报，绝不伪造
            report.update({
                "status": "QUOTE_FAILED",
                "quote_error": f"{type(exc).__name__}: {exc}",
                "quote_count": 0, "candidate_count": 0,
                "pool_size": len(eod | watchlist | portfolio),
            })
            return report
        snapshots = [map_quote_snapshot(quote, as_of) for quote in raw_quotes]
        report["status"] = "AVAILABLE"
        report["quote_count"] = len(snapshots)
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

        # 重点池 Deep：前 deep_limit 只候选的分钟结构摘要（fetch-time 事实）
        report["deep"] = await self._deep_summaries(candidates, as_of)
        return report

    async def _load_state(self) -> tuple[dict[str, Any], str | None, set, set, set]:
        """一次 UoW 会话读全：特征翻页 + EOD 候选 + Watchlist + 持仓。

        特征查询失败不阻断扫描（Overlay 对缺特征的票诚实降级）；池来源
        失败同理——Fast Lane 的主体是实时事实，历史投影缺失只记录。
        """
        features: dict[str, Any] = {}
        features_error: str | None = None
        eod: set[tuple[str, str]] = set()
        watchlist: set[tuple[str, str]] = set()
        portfolio: set[tuple[str, str]] = set()
        try:
            async with self._uow_factory() as uow:
                cursor: str | None = None
                for _ in range(max(1, self._feature_limit // _PAGE_LIMIT)):
                    page = await uow.features.query(FeatureQuery(
                        sort_by=FeatureSortField.AMOUNT, descending=True,
                        fields=_FEATURE_FIELDS, limit=_PAGE_LIMIT, cursor=cursor,
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
                recall_page = await uow.recalls.read_results(
                    recall_run_id=None, channel_code=None,
                    limit=_PAGE_LIMIT, cursor=None,
                )
                if recall_page is not None:
                    eod = {
                        (item.market, item.code) for item in recall_page.items
                    }
                for row in await uow.reads.watchlist_changes(limit=500):
                    if row.get("current_state") == "WATCHING" and row.get(
                        "security_market",
                    ) and row.get("security_code"):
                        watchlist.add(
                            (row["security_market"], row["security_code"])
                        )
                overview = await uow.reads.portfolio_overview(limit=200)
                for account in overview.get("accounts", ()):
                    for position in account.get("positions", ()):
                        if (position.get("quantity") or 0) > 0:
                            portfolio.add((
                                position.get("security_market"),
                                position.get("security_code"),
                            ))
        except Exception as exc:  # noqa: BLE001 - 历史投影缺失不阻断实时链
            features_error = f"{type(exc).__name__}: {exc}"
            portfolio = {
                (market, code) for market, code in portfolio if market and code
            }
        portfolio = {
            (market, code) for market, code in portfolio if market and code
        }
        watchlist = {
            (market, code) for market, code in watchlist if market and code
        }
        return features, features_error, eod, watchlist, portfolio

    async def _load_index_return(self, as_of: datetime) -> float | None:
        try:
            index_quote = await self._quote_provider.get_index_quote(
                _INDEX_CODE, "SH",
            )
        except Exception:  # noqa: BLE001 - 指数失败只影响相对强度一个指标
            return None
        return _index_return(map_quote_snapshot(index_quote, as_of))

    async def _deep_summaries(
        self, candidates: tuple[Any, ...], as_of: datetime,
    ) -> list[dict[str, Any]]:
        if self._deep is None or not candidates:
            return []
        summaries: list[dict[str, Any]] = []
        for candidate in candidates[: self._deep_limit]:
            try:
                structure = await self._deep.get_intraday_structure(
                    candidate.code, as_of=as_of,
                )
            except Exception as exc:  # noqa: BLE001 - 单票 Deep 失败隔离
                summaries.append({
                    "code": candidate.code, "market": candidate.market,
                    "status": "UNKNOWN",
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue
            summaries.append({
                "code": candidate.code, "market": candidate.market,
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
