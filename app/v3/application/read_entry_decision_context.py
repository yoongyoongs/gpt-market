"""RT-06：Entry Decision Context（实时方案 §8.4 / §18.2 / §27 RT-06）。

回答"现在 XX 股票能买吗"：

1. 聚合最新 Decision / EntryPlan、实时 Quote、分钟结构快照；
2. Trigger / Cancel 只做客观（价格条件）确定性评估；
3. readiness 判定：
   - 无 plan / plan 不可解析 / quote 缺失或 stale → NOT_READY
     （缺关键实时数据绝不 READY）；
   - 任一 cancel 条件客观满足 → CANCELLED；
   - 客观 trigger 全部满足（且至少一条）→ READY；
   - 其余 → WAIT_TRIGGER；
4. 响应携带 §18.4 点时字段（as_of/known_at/source/coverage/stale/quality）。

本服务只读，不创建 Decision / Trade，不改变任何状态。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

from app.v3.domain.action import EntryReadiness
from app.v3.domain.entry_plan import EntryPlanPayload, PlanCondition

_SOURCE = "entry-decision-context-v1"


def _price(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _aware(value: Any) -> datetime | None:
    """行内时点字段 → aware datetime（naive 按 UTC 补齐）；不可解析返回 None。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return None


def _evaluate_condition(condition: PlanCondition, last_price: float) -> bool | None:
    """客观条件判定；TEXT 等不可客观评估返回 None（绝不假装判定）。"""
    if not condition.objective or condition.value is None:
        return None
    if condition.kind == "PRICE_ABOVE":
        return last_price > condition.value
    if condition.kind == "PRICE_BELOW":
        return last_price < condition.value
    return None


class _ReadsRepo(Protocol):
    async def security_id_by_code(self, market: str, code: str) -> Any: ...


class _DecisionsRepo(Protocol):
    async def read_decision_state(self, security_id: Any) -> dict: ...


class _Uow(Protocol):
    reads: _ReadsRepo
    ai_imports: _DecisionsRepo

    async def __aenter__(self) -> "_Uow": ...

    async def __aexit__(self, *args) -> None: ...


class ReadEntryDecisionContextService:
    def __init__(
        self,
        uow_factory: Callable[[], _Uow],
        quote_service: Any,
        structure_service: Any,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._quote_service = quote_service
        self._structure_service = structure_service
        self._clock = clock or (lambda: datetime.now().astimezone())

    async def execute(
        self, code: str, market: str, *, as_of: datetime
    ) -> dict[str, Any]:
        known_at = self._clock()
        bundle = await self._decision_bundle(code, market)
        quote = await self._safe(lambda: self._quote_service.get_quote_snapshot(
            code, as_of=as_of,
        ))
        structure = await self._safe(lambda: self._structure_service.get_snapshot(
            code, as_of=as_of,
        ))

        decision = None
        plan_row = None
        if bundle:
            decisions = bundle.get("decisions") or ()
            # R4-P0-001：PIT 纪律——只允许 as_of 之前已知的 Decision 入选；
            # 时点不可解析的行同样排除，绝不静默采纳"未来决策"。
            current_decisions = [
                row for row in decisions
                if (ts := _aware(row.get("as_of"))) is not None and ts <= as_of
            ]
            if current_decisions:
                decision = dict(
                    max(current_decisions, key=lambda row: _aware(row.get("as_of")))
                )
            plans = [
                dict(row) for row in (bundle.get("entry_plan_versions") or ())
                if decision is None or row.get("decision_id") == decision.get("decision_id")
            ]
            # R4-P0-001：EntryPlan 生效时点同理——effective_from > as_of 的
            # 未来计划绝不入选（未标注 effective_from 视为已知，放行）。
            current_plans = [
                row for row in plans
                if row.get("effective_from") is None
                or (_eff := _aware(row.get("effective_from"))) is None
                or _eff <= as_of
            ]
            if current_plans:
                plan_row = max(current_plans, key=lambda row: row.get("version", 0))

        plan_payload: EntryPlanPayload | None = None
        plan_parse_error: str | None = None
        if plan_row is not None:
            try:
                plan_payload = EntryPlanPayload.from_plan(plan_row.get("plan") or {})
            except (ValueError, TypeError) as exc:
                plan_parse_error = str(exc)

        quote_dump = quote.model_dump() if quote is not None else None
        stale = bool(quote.stale) if quote is not None else True
        last_price = _price(quote_dump.get("last_price")) if quote_dump else None

        readiness, reason, evaluated_triggers, evaluated_cancels = self._readiness(
            plan_payload, plan_parse_error, stale, last_price,
        )

        return {
            "mode": "ENTRY",
            "code": code,
            "market": market,
            "as_of": as_of,
            "known_at": known_at,
            "source": _SOURCE,
            "coverage": "L1" if quote is not None else "NONE",
            "stale": stale,
            "quality": (quote_dump or {}).get("quality") or "UNAVAILABLE",
            "provisional": {
                "structure": bool(getattr(structure, "stale", True)),
                "quote": stale,
            },
            "decision": self._decision_summary(decision),
            "entry_plan": self._plan_summary(plan_row),
            "plan_payload": (
                plan_payload.model_dump() if plan_payload is not None else None
            ),
            "plan_parse_error": plan_parse_error,
            "quote": quote_dump,
            "structure": (
                structure.model_dump(mode="json") if structure is not None else None
            ),
            "readiness": readiness,
            "readiness_reason": reason,
            "evaluated_triggers": evaluated_triggers,
            "evaluated_cancels": evaluated_cancels,
        }

    async def _decision_bundle(self, code: str, market: str) -> dict | None:
        try:
            async with self._uow_factory() as uow:
                security_id = await uow.reads.security_id_by_code(market, code)
                if security_id is None:
                    return None
                return await uow.ai_imports.read_decision_state(security_id)
        except Exception:  # noqa: BLE001 - 上下文缺失降级为无决策，绝不 500
            return None

    async def _safe(self, fetch: Callable) -> Any:
        try:
            return await fetch()
        except Exception:  # noqa: BLE001 - 实时数据失败降级为 None + stale
            return None

    def _readiness(
        self,
        plan_payload: EntryPlanPayload | None,
        plan_parse_error: str | None,
        stale: bool,
        last_price: float | None,
    ) -> tuple[Any, str, list[dict], list[dict]]:
        evaluated_triggers: list[dict] = []
        evaluated_cancels: list[dict] = []
        if plan_payload is None:
            reason = "PLAN_PARSE_FAILED" if plan_parse_error else "NO_ENTRY_PLAN"
            return EntryReadiness.NOT_READY, reason, [], []
        if stale or last_price is None:
            # 核心约束：缺关键实时数据不得 READY
            return (
                EntryReadiness.NOT_READY, "MISSING_REALTIME_DATA", [], [],
            )
        for condition in plan_payload.cancels:
            met = _evaluate_condition(condition, last_price)
            evaluated_cancels.append({
                "kind": condition.kind, "value": condition.value, "met": met,
            })
        if any(item["met"] for item in evaluated_cancels):
            return EntryReadiness.CANCELLED, "CANCEL_CONDITION_MET", [], evaluated_cancels
        for condition in plan_payload.triggers:
            if not condition.objective:
                continue  # TEXT 条件不可客观评估，不进入判定（plan_payload 里可见）
            met = _evaluate_condition(condition, last_price)
            evaluated_triggers.append({
                "kind": condition.kind, "value": condition.value, "met": met,
            })
        objective = evaluated_triggers
        if not objective:
            return (
                EntryReadiness.WAIT_TRIGGER, "NO_OBJECTIVE_TRIGGER",
                evaluated_triggers, evaluated_cancels,
            )
        if all(item["met"] for item in objective):
            return (
                EntryReadiness.READY, "OBJECTIVE_TRIGGERS_MET",
                evaluated_triggers, evaluated_cancels,
            )
        return (
            EntryReadiness.WAIT_TRIGGER, "TRIGGERS_NOT_MET",
            evaluated_triggers, evaluated_cancels,
        )

    @staticmethod
    def _decision_summary(decision: dict | None) -> dict | None:
        if decision is None:
            return None
        return {
            "decision_id": str(decision.get("decision_id")),
            "as_of": decision.get("as_of"),
        }

    @staticmethod
    def _plan_summary(plan_row: dict | None) -> dict | None:
        if plan_row is None:
            return None
        return {
            "entry_plan_id": str(plan_row.get("entry_plan_id")),
            "decision_id": str(plan_row.get("decision_id")),
            "version": plan_row.get("version"),
            "effective_from": plan_row.get("effective_from"),
            "expected_horizon": plan_row.get("expected_horizon"),
        }
