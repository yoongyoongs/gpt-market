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

R4-P1-004：聚合完整事实包（复验 §16）——在原有 decision/plan/quote/
structure 之上补 market_regime / feature_eod / latest_recall /
latest_raw_opportunity / latest_action / latest_entry_assessment /
attention_events / data_quality；无数据源的字段（fundamental）
显式 NOT_AVAILABLE + reason，绝不静默缺省。

R5-P1-006（§64）：Evidence 真实调用 Evidence Read（retrieve_view 按
subject 精确匹配），latest_recall / latest_raw_opportunity 改为
Security-specific 查询（SQL 端过滤，禁止取前 200 条客户端找）。

本服务只读，不创建 Decision / Trade，不改变任何状态。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

from app.v3.domain.action import EntryReadiness
from app.v3.domain.evidence import EvidenceReadQuery
from app.v3.domain.entry_plan import EntryPlanPayload, PlanCondition

_SOURCE = "entry-decision-context-v1"


def _not_available(reason: str) -> dict[str, str]:
    return {"status": "NOT_AVAILABLE", "reason": reason}


def _unknown(reason: str) -> dict[str, str]:
    return {"status": "UNKNOWN", "reason": reason}


def _dump(value: Any) -> Any:
    """pydantic → JSON 兼容 dict；普通对象原样返回。"""
    if value is None or isinstance(value, (dict, list, str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _jsonify(value: Any) -> Any:
    """普通 ORM 列 dict → JSON 兼容（UUID/datetime/Decimal）。"""
    from decimal import Decimal
    from uuid import UUID

    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _item_bundle(missing_reason: str, run_reason: str) -> Callable:
    """R5-P1-006/§64：Security-specific 读取结果打包——
    None（无 Published run）与空（run 内无此券）区分报告，绝不混同。"""
    def transform(items: Any) -> Any:
        if items is None:
            return _not_available(run_reason)
        if not items:
            return _not_available(missing_reason)
        return {
            "status": "AVAILABLE",
            "count": len(items),
            "items": [_dump(item) for item in items],
        }
    return transform


def _evidence_bundle(page: Any, known_ats: list[datetime]) -> Any:
    """R5-P1-006/§64：Evidence 真实聚合——retrieve_view 按 subject
    精确匹配（DIRECT/CONFIRMED_LINK），绝不再返回
    NO_EVIDENCE_READ_API 占位；record.known_at 纳入顶层聚合。"""
    views = getattr(page, "views", ()) or ()
    if not views:
        return _not_available("NO_EVIDENCE_FOR_SECURITY")
    items = []
    for view in views:
        known = _aware(getattr(view.record, "known_at", None))
        if known is not None:
            known_ats.append(known)
        items.append(_dump(view))
    return {"status": "AVAILABLE", "count": len(items), "items": items}


def _latest_action(pipeline: Any) -> Any:
    """read_pipeline 首条 = 最新 ActionCandidate（按 as_of desc）。"""
    if not pipeline:
        return _not_available("NO_ACTION_CANDIDATE")
    first = pipeline[0]
    action = _jsonify(first.get("action"))
    latest_entry = None
    entries = first.get("entries") or ()
    if entries:
        latest_entry = _jsonify(entries[0])
    return {
        "status": "AVAILABLE",
        "raw_opportunity_id": str(first.get("raw_opportunity_id")),
        "action": action,
        "latest_entry": latest_entry,
    }


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
        # R5-P0-001/§8.1：as_of 是本次请求起点（request_started_at，T0）。
        # 顶层 known_at 绝不在 fetch 前敲定——聚合完成后按 §7.1 重算：
        # context_as_of = max(T0, 所有 component known_at)；
        # context_known_at = max(所有 component known_at, 聚合完成时刻)。
        request_started_at = as_of
        component_known: list[datetime] = [request_started_at]
        bundle, security_id = await self._decision_bundle(code, market)
        quote = await self._safe(lambda: self._quote_service.get_quote_snapshot(
            code, as_of=as_of,
        ))
        structure = await self._safe(lambda: self._structure_service.get_snapshot(
            code, as_of=as_of,
        ))
        facts, fact_known_ats = await self._eod_facts(
            security_id, code, market, as_of,
        )
        component_known.extend(fact_known_ats)
        quote_known = getattr(quote, "known_at", None) if quote is not None else None
        if quote_known is not None:
            component_known.append(quote_known)
        structure_known = getattr(structure, "known_at", None)
        if structure is not None and structure_known is not None:
            component_known.append(structure_known)
        final_as_of = max(component_known)
        final_known_at = max([*component_known, self._clock()])

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
            "as_of": final_as_of,
            "known_at": final_known_at,
            "source": _SOURCE,
            "coverage": "L1" if quote is not None else "NONE",
            "stale": stale,
            "quality": (quote_dump or {}).get("quality") or "UNAVAILABLE",
            "provisional": {
                "structure": bool(getattr(structure, "stale", True)),
                "quote": stale,
            },
            "security": {
                "code": code, "market": market,
                "security_id": str(security_id) if security_id else None,
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
            "market_regime": facts["market_regime"],
            "feature_eod": facts["feature_eod"],
            "latest_recall": facts["latest_recall"],
            "latest_raw_opportunity": facts["latest_raw_opportunity"],
            "latest_action": facts["latest_action"],
            "latest_entry_assessment": facts["latest_entry_assessment"],
            "fundamental": facts["fundamental"],
            "evidence": facts["evidence"],
            "attention_events": facts["attention_events"],
            "data_quality": facts["data_quality"],
        }

    async def _eod_facts(
        self, security_id: Any, code: str, market: str, as_of: datetime,
    ) -> tuple[dict[str, Any], list[datetime]]:
        """R4-P1-004：一次 UoW 会话补齐 §16 缺口字段。

        每个来源独立 try——单个 repo 失败降级 UNKNOWN 并回滚会话，
        不拖垮其余事实；无数据源的字段显式 NOT_AVAILABLE + reason。
        返回 (facts, known_ats)：known_ats 供顶层 §59.3 聚合
        context.known_at >= max(component known_at)。
        """
        facts: dict[str, Any] = {
            "fundamental": _not_available("NO_FUNDAMENTAL_SOURCE"),
        }
        for field in (
            "market_regime", "feature_eod", "latest_recall",
            "latest_raw_opportunity", "latest_action",
            "latest_entry_assessment", "attention_events", "data_quality",
            "evidence",
        ):
            facts.setdefault(field, _not_available("SOURCE_NOT_BOUND"))
        known_ats: list[datetime] = []
        if security_id is None:
            for field in (
                "market_regime", "feature_eod", "latest_recall",
                "latest_raw_opportunity", "latest_action",
                "latest_entry_assessment", "attention_events", "data_quality",
                "evidence",
            ):
                facts[field] = _not_available("SECURITY_UNKNOWN")
            return facts, known_ats

        async def grab(field: str, fetch: Callable, transform: Callable) -> None:
            try:
                raw = await fetch()
                known = getattr(raw, "known_at", None)
                if _aware(known) is not None:
                    known_ats.append(known)
                facts[field] = transform(raw)
            except Exception as exc:  # noqa: BLE001 - 单来源失败不拖垮整包
                facts[field] = _unknown(f"{type(exc).__name__}: {exc}")
                try:
                    await uow.rollback()
                except Exception:  # noqa: BLE001
                    pass

        async with self._uow_factory() as uow:
            await grab(
                "market_regime",
                lambda: uow.features.latest_regime(),
                lambda regime: (
                    _not_available("NO_PUBLISHED_REGIME")
                    if regime is None else _dump(regime)
                ),
            )
            await grab(
                "feature_eod",
                lambda: uow.features.latest_security_feature(
                    security_id, as_of=as_of,
                ),
                lambda view: (
                    _not_available("NO_PUBLISHED_FEATURE_FOR_SECURITY")
                    if view is None else _dump(view)
                ),
            )
            await grab(
                "latest_recall",
                lambda: uow.recalls.latest_recall_for_security(
                    market=market, code=code,
                ),
                _item_bundle(
                    "NOT_IN_LATEST_RECALL_RESULTS", "NO_PUBLISHED_RECALL_RUN",
                ),
            )
            await grab(
                "latest_raw_opportunity",
                lambda: uow.recalls.latest_raw_opportunity_for_security(
                    market=market, code=code,
                ),
                _item_bundle(
                    "NOT_IN_LATEST_RAW_OPPORTUNITY",
                    "NO_PUBLISHED_RECALL_RUN",
                ),
            )
            # R5-P1-006/§64：Evidence 真实调用现有 Evidence Read 能力，
            # 按 subject（SECURITY:{market}:{code}）精确匹配。
            await grab(
                "evidence",
                lambda: uow.evidence.retrieve_view(query=EvidenceReadQuery(
                    subject_type="SECURITY",
                    subject_id=f"{market}:{code}",
                    as_of=as_of,
                    include_candidates=False,
                    limit=50,
                )),
                lambda page: _evidence_bundle(page, known_ats),
            )
            await grab(
                "latest_action",
                lambda: uow.actions.read_pipeline(security_id, limit=10),
                _latest_action,
            )
            facts["latest_entry_assessment"] = (
                facts["latest_action"].get("latest_entry") if isinstance(
                    facts.get("latest_action"), dict,
                ) else None
            ) or _not_available("NO_ENTRY_ASSESSMENT")
            await grab(
                "attention_events",
                lambda: uow.attention.open_events(codes=[code], limit=20),
                lambda events: [
                    _dump(event) for event in events
                ] or _not_available("NO_OPEN_ATTENTION_EVENTS"),
            )
            await grab("data_quality", lambda: uow.reads.data_quality(), _dump)
        return facts, known_ats

    async def _decision_bundle(
        self, code: str, market: str,
    ) -> tuple[dict | None, Any]:
        try:
            async with self._uow_factory() as uow:
                security_id = await uow.reads.security_id_by_code(market, code)
                if security_id is None:
                    return None, None
                return (
                    await uow.ai_imports.read_decision_state(security_id),
                    security_id,
                )
        except Exception:  # noqa: BLE001 - 上下文缺失降级为无决策，绝不 500
            return None, None

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
