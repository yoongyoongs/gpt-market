"""RT-07：Position Decision Context（实时方案 §9.4 / §18.2 / §27 RT-07）。

回答"现在卖不卖"的确定性底座：在完整 Position Context（RC-04D）之上补充——

- source 等点时字段（§18.4）；
- objective_sell_facts：stop/target 相对最新价的客观事实，只陈述、
  绝不产生卖出建议；卖出判断永远由 AI/人做，成交必须走 Trade Draft；
- R3-P1-007：只有新鲜实时快照才产生确定性触达事实——Quote stale 或
  LKG 价一律显式 UNKNOWN + assessment_reason，绝不假装是当前判断。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def _price(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


class ReadPositionDecisionContextService:
    def __init__(
        self,
        context_service: Any,
        *,
        source: str = "position-decision-context-v1",
    ) -> None:
        self._context_service = context_service
        self._source = source

    async def execute(
        self,
        account_id: Any,
        code: str,
        market: str | None = None,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        context = await self._context_service.execute(
            account_id, code, market, as_of=as_of,
        )
        context["source"] = self._source
        context["objective_sell_facts"] = self._objective_facts(context)
        return context

    @staticmethod
    def _objective_facts(context: dict[str, Any]) -> dict[str, Any]:
        market = context.get("market") or {}
        levels = context.get("levels") or {}
        last_price = _price(market.get("latest_price"))
        stop = _price(levels.get("stop"))
        target = _price(levels.get("target"))
        stop_hit: bool | None = None
        target_hit: bool | None = None
        # R3-P1-007：只有 **新鲜的实时快照** 才允许产生确定性 stop/target
        # 触达事实——Quote stale → STALE_REALTIME_QUOTE；最新价根本不是
        # 实时快照（FEATURE_LKG 等）→ NO_REALTIME_PRICE。LKG close 只可
        # 作展示/PnL 参考，绝不冒充"现在是否触达"的判断（与 Entry Context
        # stale 不得 READY 的安全纪律一致）。
        price_source = market.get("price_source")
        reason: str | None = None
        if price_source != "REALTIME_QUOTE":
            reason = "NO_REALTIME_PRICE"
        elif bool(market.get("quote_stale")):
            reason = "STALE_REALTIME_QUOTE"
        if reason is None:
            if last_price is not None and stop is not None:
                stop_hit = last_price <= stop
            if last_price is not None and target is not None:
                target_hit = last_price >= target
        return {
            "last_price": last_price,
            "price_source": price_source,
            "price_known_at": market.get("price_known_at"),
            "eod_feature_close": _price(market.get("eod_feature_close")),
            "stop": stop,
            "target": target,
            "stop_hit": stop_hit,
            "target_hit": target_hit,
            "assessment_reason": reason,
            "invalidation": levels.get("invalidation"),
        }
