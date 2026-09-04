"""Position Context 全量载荷（RC-05B / CTX-001）。

目标：ChatGPT 读取一个 Position Context 即可完成持仓 Review，无需用户
手工补价格/K线/成本。所有分节显式标注来源与时点语义；缺失一律
UNKNOWN/NOT_AVAILABLE + reason，绝不伪造：
- quantity/cost：Position Projection（Ledger 重放，只含已确认事实）；
- latest price：绑定 quote_service 时为实时快照（REALTIME_QUOTE，
  suspended/失败如实回退 FEATURE_LKG）；已发布 Feature 的 LKG close
  始终作为独立 EOD 事实并列返回（eod_feature_close + known_at 标注），
  两个时间戳并存，绝不混用；
- support/resistance：版本化结构化计算 support-resistance-20d-v1；
- 5m/15m/60m：DeepMarketData（分钟事实=抓取时点事实，precision LIMITED）；
- fundamental/industry：当前无可靠数据源，显式 NOT_AVAILABLE。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.utils.time import SHANGHAI
from app.v3.application.calculate_features import CalculateSecurityFeatureService
from app.v3.domain.evidence import EvidenceReadQuery

SUPPORT_RESISTANCE_VERSION = "support-resistance-20d-v1"
SUPPORT_RESISTANCE_LOOKBACK = 20
HOLDING_SESSION_MAX_DAYS = 1500


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "UNKNOWN", "reason": reason}


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


class ReadPositionContextService:
    def __init__(
        self,
        uow_factory: Callable,
        *,
        clock: Callable[[], datetime] | None = None,
        calendar: Any = None,
        deep_market_data: Any = None,
        quote_service: Any = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._calendar = calendar
        self._deep = deep_market_data
        # NEW-CTX-001：实时 Quote Overlay（IntradayMarketDataService 等协议：
        # get_quote_snapshot(code, as_of=...) → last_price/known_at/suspended）
        self._quote_service = quote_service

    async def execute(
        self,
        account_id: UUID,
        code: str,
        market: str | None = None,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        as_of = as_of or self._clock()
        async with self._uow_factory() as uow:
            facts = await uow.portfolios.position_context(account_id, code, market)
            security_id = facts["security"]["security_id"]
            feature = await uow.features.latest_security_feature(
                security_id, as_of=as_of
            )
            regime = await uow.features.latest_regime()
            daily_revisions = await uow.bars.latest_daily_revisions(
                (security_id,), as_of=as_of
            )
            evidence_page = await uow.evidence.retrieve_view(
                query=EvidenceReadQuery(
                    subject_type="SECURITY",
                    subject_id=f"{facts['security']['market']}:{facts['security']['code']}",
                    as_of=as_of,
                    include_candidates=False,
                    limit=50,
                )
            )
        position = facts["position"]
        plans = list(facts["entry_plans"])
        trades = list(facts["trades"])
        quantity = Decimal(str(position["quantity"]))
        average_cost = Decimal(str(position["average_cost"]))

        price_section = await self._price_section(
            feature, quantity, average_cost,
            code=facts["security"]["code"], as_of=as_of,
        )
        holding = self._holding_section(trades, position, as_of)
        entry_plan = self._entry_plan_section(plans, trades)
        levels = self._levels_section(
            entry_plan, daily_revisions[0] if daily_revisions else None, as_of
        )
        multi_timeframe = await self._multi_timeframe_section(
            facts["security"]["code"], feature, as_of
        )
        return {
            "security": facts["security"],
            "as_of": _iso(as_of),
            "known_at": _iso(self._clock()),
            "position": {
                "quantity": quantity,
                "cost_method": "WEIGHTED_AVERAGE",
                "cost_basis": Decimal(str(position["cost_basis"])),
                "average_cost": average_cost,
                "realized_pnl": Decimal(str(position["realized_pnl"])),
            },
            "market": price_section,
            "holding": holding,
            "trades": trades,
            "entry_plan": entry_plan,
            "levels": levels,
            "multi_timeframe": multi_timeframe,
            "fundamental": {"status": "NOT_AVAILABLE", "reason": "NO_RELIABLE_SOURCE"},
            "industry": {"status": "NOT_AVAILABLE", "reason": "NO_RELIABLE_SOURCE"},
            "market_regime": None if regime is None else {
                "regime_snapshot_id": str(regime.regime_snapshot_id),
                "as_of": _iso(regime.as_of),
                "known_at": _iso(regime.known_at),
                "index_states": regime.index_states,
                "breadth": regime.breadth,
            },
            "evidence": {
                "boundary": "UNTRUSTED_DATA",
                "count": len(evidence_page.views),
                "items": [
                    {
                        "evidence_id": str(view.record.evidence_id),
                        "known_at": _iso(view.record.known_at),
                        "side": str(
                            view.record.normalized_payload.get("side", "NEUTRAL")
                        ),
                    }
                    for view in evidence_page.views
                ],
            },
            "risk": self._risk_section(feature, price_section, levels, quantity),
            "time_efficiency": self._time_efficiency_section(entry_plan, holding),
            "latest_position_review": facts["latest_position_review"],
            "previous_position_review_id": (
                str(facts["latest_position_review"]["previous_position_review_id"])
                if facts["latest_position_review"]
                and facts["latest_position_review"].get("previous_position_review_id")
                is not None
                else None
            ),
            "data_quality": self._quality_section(position, feature, daily_revisions),
        }

    async def _price_section(
        self,
        feature,
        quantity: Decimal,
        average_cost: Decimal,
        *,
        code: str,
        as_of: datetime,
    ) -> dict:
        """NEW-CTX-001：market.latest_price 优先实时 Quote（REALTIME_QUOTE）；
        未绑定/失败/停牌时回退 FEATURE_LKG 并如实标注 quote_status。
        EOD Feature close 永远作为独立事实（eod_feature_close）并列返回。"""
        if feature is None:
            section: dict[str, Any] = _unknown("NO_FEATURE_PRICE")
        else:
            section = {
                "status": "AVAILABLE",
                "latest_price": Decimal(str(feature.close)),
                "price_source": "FEATURE_LKG",
                "price_known_at": _iso(feature.as_of),
            }
        if self._quote_service is not None:
            section = await self._overlay_realtime_quote(section, code, as_of)
            if feature is not None:
                section["eod_feature_close"] = Decimal(str(feature.close))
                section["eod_feature_known_at"] = _iso(feature.as_of)
        if section.get("status") != "AVAILABLE":
            return section
        price = section["latest_price"]
        if quantity == 0:
            section["unrealized_pnl"] = Decimal("0")
            section["return_pct"] = None
        elif average_cost > 0:
            section["unrealized_pnl"] = (price - average_cost) * quantity
            section["return_pct"] = float(price / average_cost - 1)
        return section

    async def _overlay_realtime_quote(
        self, section: dict, code: str, as_of: datetime,
    ) -> dict:
        try:
            quote = await self._quote_service.get_quote_snapshot(code, as_of=as_of)
        except Exception as exc:  # noqa: BLE001 - 行情失败回退 LKG 并如实标注
            section["quote_status"] = f"UNKNOWN: {type(exc).__name__}: {exc}"
            return section
        if getattr(quote, "suspended", False):
            section["quote_status"] = "SUSPENDED"
        elif quote.last_price is None:
            section["quote_status"] = "NO_LAST_PRICE"
        else:
            section.update({
                "status": "AVAILABLE",
                "latest_price": Decimal(str(quote.last_price)),
                "price_source": "REALTIME_QUOTE",
                "price_known_at": _iso(quote.known_at),
                "quote_source": quote.source,
                "quote_stale": bool(getattr(quote, "stale", False)),
            })
        return section

    def _holding_section(self, trades, position, as_of: datetime) -> dict:
        buy_times = [
            item["trade_time"] for item in trades if item.get("side") == "BUY"
        ]
        first_buy = min(buy_times) if buy_times else None
        return {
            "first_buy_time": first_buy,
            "holding_sessions": self._holding_sessions(first_buy, as_of),
        }

    def _holding_sessions(self, first_buy: str | None, as_of: datetime) -> dict | int:
        if first_buy is None:
            return _unknown("NO_BUY_TRADE")
        if self._calendar is None:
            return _unknown("CALENDAR_NOT_BOUND")
        try:
            start = date.fromisoformat(str(first_buy)[:10])
            # R4-P2-009：交易日归属必须按 Asia/Shanghai，服务器本地时区
            # （如 UTC）在北京时间凌晨附近会偏一天
            end = (
                as_of.astimezone(SHANGHAI).date()
                if as_of.tzinfo else as_of.date()
            )
        except ValueError:
            return _unknown("INVALID_TRADE_TIME")
        if (end - start).days > HOLDING_SESSION_MAX_DAYS:
            return _unknown("CALENDAR_COVERAGE_EXCEEDED")
        sessions = 0
        current = start + timedelta(days=1)
        while current <= end:
            try:
                if self._calendar.is_trading_day(current):
                    sessions += 1
            except Exception:
                return _unknown("CALENDAR_COVERAGE_EXCEEDED")
            current += timedelta(days=1)
        return sessions

    @staticmethod
    def _entry_plan_section(plans: list[dict], trades: list[dict]) -> dict:
        if not plans:
            return {
                "original": _unknown("NO_ENTRY_PLAN"),
                "current": _unknown("NO_ENTRY_PLAN"),
                "trade_bound": _unknown("NO_TRADE_PLAN_BINDING"),
            }
        ordered = sorted(plans, key=lambda item: item["version"])
        bound = [
            item for item in trades
            if item.get("entry_plan_id") is not None
        ]
        latest_bound = bound[-1] if bound else None
        trade_bound: Any = (
            {
                "entry_plan_id": latest_bound["entry_plan_id"],
                "version": latest_bound.get("entry_plan_version"),
            }
            if latest_bound is not None
            else _unknown("NO_TRADE_PLAN_BINDING")
        )
        return {
            "original": ordered[0],
            "current": ordered[-1],
            "trade_bound": trade_bound,
        }

    def _levels_section(self, entry_plan: dict, revision, as_of: datetime) -> dict:
        if revision is None:
            support = resistance = _unknown("NO_DAILY_BARS")
        else:
            bars = [
                bar for bar in revision.bars
                if bar.bar_time <= as_of and not bar.provisional
            ][-SUPPORT_RESISTANCE_LOOKBACK:]
            if len(bars) < SUPPORT_RESISTANCE_LOOKBACK:
                support = resistance = _unknown("INSUFFICIENT_BARS")
            else:
                support = min(bar.low for bar in bars)
                resistance = max(bar.high for bar in bars)
        current_plan = (
            entry_plan["current"]
            if "plan" in entry_plan.get("current", {})
            else {}
        )
        plan_values = current_plan.get("plan", {})
        stop = (
            plan_values["stop_loss"] if "stop_loss" in plan_values
            else _unknown("PLAN_HAS_NO_STOP_TARGET")
        )
        target = (
            plan_values["take_profit"] if "take_profit" in plan_values
            else _unknown("PLAN_HAS_NO_STOP_TARGET")
        )
        # §14.1：失效条件随计划透出，计划缺失时诚实 UNKNOWN
        invalidation = (
            plan_values["invalidation"] if "invalidation" in plan_values
            else _unknown("PLAN_HAS_NO_INVALIDATION")
        )
        return {
            "support": support,
            "resistance": resistance,
            "stop": stop,
            "target": target,
            "invalidation": invalidation,
            "calculation_version": SUPPORT_RESISTANCE_VERSION,
        }

    async def _multi_timeframe_section(
        self, code: str, feature, as_of: datetime
    ) -> dict:
        if feature is None:
            weekly = daily = _unknown("NO_FEATURE")
            state: Any = _unknown("NO_FEATURE")
            rule: Any = None
        else:
            states = feature.features or {}
            weekly = {
                "state": states.get("weekly_trend_state") or "UNKNOWN",
                "known_at": _iso(feature.as_of),
            }
            daily = {
                "state": states.get("daily_trend_state") or "UNKNOWN",
                "known_at": _iso(feature.as_of),
            }
            # §14.2：与特征计算共用同一条确定性合成规则（"下降趋势中的反弹"等）
            state, rule = CalculateSecurityFeatureService._multi_timeframe(
                daily["state"], weekly["state"]
            )
        section: dict[str, Any] = {
            "weekly": weekly, "daily": daily, "state": state, "rule": rule,
        }
        # R3-P2-008：get_intraday_structure 一次已取全 60m/15m/5m——
        # 只调用一次（原实现每周期重复抓取 → 3×Provider 请求放大，且
        # 三次请求时间点不同可能得到不同快照）。
        structure: Any = None
        deep_error: str | None = None
        if self._deep is not None:
            try:
                structure = await self._deep.get_intraday_structure(
                    code, as_of=as_of
                )
            except Exception as exc:  # noqa: BLE001 - 深度数据失败显式 UNKNOWN
                deep_error = f"{type(exc).__name__}: {exc}"
        periods: dict[str, Any] = {}
        if structure is not None:
            raw = (
                structure.get("periods")
                if isinstance(structure, dict)
                else getattr(structure, "periods", None)
            )
            if isinstance(raw, dict):
                periods = raw
        for period in ("60m", "15m", "5m"):
            payload = periods.get(period)
            if payload is not None:
                section[period] = payload
            elif deep_error is not None:
                section[period] = {
                    **_unknown(deep_error),
                    "precision": "UNKNOWN",
                }
            elif self._deep is None:
                section[period] = _unknown("DEEP_MARKET_DATA_NOT_BOUND")
            else:
                section[period] = {
                    **_unknown("PERIOD_NOT_IN_STRUCTURE"),
                    "precision": "UNKNOWN",
                }
        return section

    @staticmethod
    def _risk_section(feature, price_section, levels, quantity: Decimal) -> dict:
        section: dict[str, Any] = {}
        if feature is not None and feature.atr_pct is not None:
            section["atr_pct"] = feature.atr_pct
        stop = levels.get("stop")
        if (
            price_section.get("status") == "AVAILABLE"
            and isinstance(stop, (str, int, float, Decimal))
        ):
            try:
                price = price_section["latest_price"]
                section["stop_distance_pct"] = float(
                    (price - Decimal(str(stop))) / price
                )
            except Exception:
                pass
        section["quantity"] = quantity
        return section

    @staticmethod
    def _time_efficiency_section(entry_plan: dict, holding: dict) -> dict:
        current = entry_plan.get("current", {})
        return {
            "expected_horizon": current.get("expected_horizon"),
            "holding_sessions": holding.get("holding_sessions"),
        }

    @staticmethod
    def _quality_section(position, feature, daily_revisions) -> dict:
        section: dict[str, Any] = {
            "projection": {
                "projection_version": position.get("projection_version"),
                "input_hash": position.get("input_hash"),
                "rebuilt_at": _iso(position.get("rebuilt_at")),
            },
            "daily_revision_count": len(daily_revisions),
        }
        if feature is not None:
            section["feature"] = {
                "coverage": feature.coverage,
                "stale": feature.stale,
                "missing_fields": list(feature.missing_fields),
                "source_errors": list(feature.source_errors),
            }
        else:
            section["feature"] = _unknown("NO_FEATURE_PRICE")
        return section
