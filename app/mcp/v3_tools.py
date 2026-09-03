"""RT-08：ChatGPT/MCP V3 只读工具注册（实时方案 §19/§27 RT-08）。

把 §19 的 V3 READ 工具挂到现有 MCP server 上：

- MARKET_READ 组：市场概览、盘中状态、机会扫描、个股/持仓决策上下文、
  分钟结构、观察池、Attention 事件；
- PORTFOLIO_READ 组（部署级开关 V3_MCP_PORTFOLIO_ENABLED）：持仓上下文、
  Position Decision Context。

不变量：这些工具全部只读（READ），绝不产生建议或 Trade；
建议由 AI 基于 Context 得出，成交必须走 Trade Draft。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _trading_day_or_false(calendar: Any, day: date) -> bool:
    """日历覆盖范围外（如远期）按非交易日处理，不抛错。"""
    try:
        return bool(calendar.is_trading_day(day))
    except Exception:
        return False


def register_v3_tools(mcp: Any, container: Any) -> list[str]:
    """在 V3 启用时注册 §19 只读工具；返回已注册工具名列表。"""
    v3 = getattr(container, "v3", None)
    if v3 is None or not getattr(v3, "enabled", False):
        return []

    from app.v3.application.intraday_market_data import IntradayMarketDataService
    from app.v3.application.intraday_structure_snapshot import (
        IntradayStructureSnapshotService,
    )
    from app.v3.application.manage_decisions import DecisionStateService
    from app.v3.application.market_intraday_status import (
        MarketIntradayStatusService,
    )
    from app.v3.application.pipeline_eod_latest import PipelineEodLatestService
    from app.v3.application.read_attention import ReadAttentionEventsService
    from app.v3.application.read_entry_decision_context import (
        ReadEntryDecisionContextService,
    )
    from app.v3.application.read_position_context import ReadPositionContextService
    from app.v3.application.read_position_decision_context import (
        ReadPositionDecisionContextService,
    )
    from app.v3.infrastructure.providers.exchange_calendar import (
        ExchangeCalendarsAShareCalendar,
    )

    from app.container import container as root_container

    uow_factory = getattr(v3, "uow", None)
    calendar = ExchangeCalendarsAShareCalendar()
    _cache: dict[str, Any] = {}

    def _bars_service() -> Any:
        if "bars" not in _cache:
            # R3-P1-006：实时主入口走 ProviderManager（东财/腾讯 fallback）
            _cache["bars"] = IntradayMarketDataService(root_container.provider_manager)
        return _cache["bars"]

    def _structure_service() -> Any:
        if "structure" not in _cache:
            _cache["structure"] = IntradayStructureSnapshotService(
                _bars_service(),
            )
        return _cache["structure"]

    def _intraday_status() -> Any:
        if "status" not in _cache:
            _cache["status"] = MarketIntradayStatusService(
                clock=_utcnow,
                is_trading_day=lambda day: _trading_day_or_false(calendar, day),
            )
        return _cache["status"]

    def _pipeline_latest() -> Any:
        if "pipeline" not in _cache:
            _cache["pipeline"] = PipelineEodLatestService(
                uow_factory,
                clock=_utcnow,
            )
        return _cache["pipeline"]

    def _attention_read() -> Any:
        if "attention" not in _cache:
            _cache["attention"] = ReadAttentionEventsService(
                uow_factory,
                clock=_utcnow,
            )
        return _cache["attention"]

    def _entry_context() -> Any:
        if "entry" not in _cache:
            _cache["entry"] = ReadEntryDecisionContextService(
                uow_factory,
                _bars_service(),
                _structure_service(),
                clock=_utcnow,
            )
        return _cache["entry"]

    def _position_context_service() -> Any:
        """NEW-CTX-002：MCP 主路径同样绑定 Calendar/Deep/实时 Quote。"""
        if "position_context" not in _cache:
            from app.v3.application.deep_market_data import DeepMarketDataService

            _cache["position_context"] = ReadPositionContextService(
                uow_factory,
                calendar=calendar,
                deep_market_data=DeepMarketDataService(
                    root_container.provider_manager, source="legacy-provider",
                ),
                quote_service=_bars_service(),
            )
        return _cache["position_context"]

    registered: list[str] = []

    def _tool(func):
        mcp.tool()(func)
        registered.append(func.__name__)
        return func

    @_tool
    async def v3_market_overview() -> dict:
        """V3 EOD 市场概览：最新已发布 market regime（只读）。"""
        async with uow_factory() as uow:
            regime = await uow.features.latest_regime()
        if regime is None:
            return {
                "source": "market-overview-v1",
                "status": "EMPTY",
                "detail": "no published regime yet",
            }
        return {"source": "market-overview-v1", "known_at": _utcnow(), "regime": regime}

    @_tool
    async def v3_market_intraday_status() -> dict:
        """V3 盘中状态：当前交易时段（OPEN/LUNCH_BREAK/PRE_OPEN/CLOSED）。"""
        return await _intraday_status().execute()

    @_tool
    async def v3_pipeline_eod_latest() -> dict:
        """V3 EOD 流水线最近一次各 job 运行状态（只读）。"""
        return await _pipeline_latest().execute()

    @_tool
    async def v3_scan_opportunities(
        mode: str = "EOD",
        recall_run_id: str | None = None,
        limit: int = 50,
    ) -> dict:
        """V3 机会扫描结果读取。mode=EOD 读最近已发布 raw opportunities；
        INTRADAY 快线扫描尚未接入，诚实返回 NOT_AVAILABLE。"""
        normalized = mode.strip().upper()
        if normalized == "INTRADAY":
            return {
                "source": "scan-opportunities-v1",
                "mode": "INTRADAY",
                "status": "NOT_AVAILABLE",
                "detail": "intraday scan pipeline is not wired yet",
            }
        if normalized != "EOD":
            return {
                "source": "scan-opportunities-v1",
                "mode": mode,
                "status": "INVALID_MODE",
                "detail": "mode must be EOD or INTRADAY",
            }
        run_id = None
        if recall_run_id:
            from uuid import UUID

            run_id = UUID(recall_run_id)
        async with uow_factory() as uow:
            page = await uow.recalls.read_raw(
                recall_run_id=run_id,
                limit=max(1, min(limit, 200)),
                cursor=None,
            )
        if page is None:
            return {
                "source": "scan-opportunities-v1",
                "mode": "EOD",
                "status": "EMPTY",
                "detail": "no published recall run",
            }
        return {
            "source": "scan-opportunities-v1",
            "mode": "EOD",
            "status": "AVAILABLE",
            "page": page,
        }

    @_tool
    async def v3_stock_decision_context(
        code: str,
        market: str = "SZ",
        mode: str = "ENTRY",
        account_id: str | None = None,
    ) -> dict:
        """V3 个股决策上下文。mode=ENTRY 回答"现在能买吗"（客观 Trigger/
        Cancel 评估）；mode=REVIEW 需 account_id，回答持仓相关上下文。"""
        normalized = mode.strip().upper()
        if normalized == "REVIEW":
            if not account_id:
                return {
                    "source": "stock-decision-context-v1",
                    "mode": "REVIEW",
                    "status": "ACCOUNT_REQUIRED",
                    "detail": "REVIEW mode requires account_id; "
                    "use v3_position_decision_context",
                }
            from uuid import UUID

            service = ReadPositionDecisionContextService(
                _position_context_service(),
            )
            return await service.execute(
                UUID(account_id),
                code,
                market,
                as_of=_utcnow(),
            )
        return await _entry_context().execute(
            code,
            market,
            as_of=_utcnow(),
        )

    @_tool
    async def v3_stock_intraday_structure(code: str) -> dict:
        """V3 个股分钟级结构快照（周/日/盘中 period 结构，只读事实）。"""
        snapshot = await _structure_service().get_snapshot(code, as_of=_utcnow())
        if hasattr(snapshot, "model_dump"):
            return snapshot.model_dump(mode="json")
        return {"source": "stock-intraday-structure-v1", "snapshot": snapshot}

    @_tool
    async def v3_watchlist(state: str | None = None, limit: int = 50) -> dict:
        """V3 观察池（Decision State）只读列表。"""
        return await DecisionStateService(uow_factory).read_watchlist(
            state,
            max(1, min(limit, 200)),
        )

    @_tool
    async def v3_attention_events(
        codes: list[str] | None = None,
        event_types: list[str] | None = None,
        limit: int = 100,
    ) -> dict:
        """V3 Attention 事件（只读，OPEN 状态）：止损/触发/异动提醒。"""
        return await _attention_read().execute(
            codes=codes,
            event_types=event_types,
            limit=max(1, min(limit, 500)),
        )

    if _truthy_env("V3_MCP_PORTFOLIO_ENABLED"):

        @_tool
        async def v3_position_context(
            account_id: str,
            code: str,
            market: str | None = None,
        ) -> dict:
            """V3 完整持仓上下文（成本/仓位/市场/levels，只读）。"""
            from uuid import UUID

            return await _position_context_service().execute(
                UUID(account_id),
                code,
                market,
            )

        @_tool
        async def v3_position_decision_context(
            account_id: str,
            code: str,
            market: str | None = None,
        ) -> dict:
            """V3 卖出决策上下文：完整持仓上下文 + stop/target 客观事实
            （只陈述、绝无建议；卖出决策由 AI/人做）。"""
            from uuid import UUID

            service = ReadPositionDecisionContextService(
                _position_context_service(),
            )
            return await service.execute(
                UUID(account_id),
                code,
                market,
                as_of=_utcnow(),
            )

    return registered
