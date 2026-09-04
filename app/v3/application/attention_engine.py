"""RT-04：Trigger / Attention Engine（实时方案 §10 / §27 RT-04）。

系统实时判断"哪些客观条件变了，值得让 AI 重新看"：

- 只做确定性评估：stop/target 命中与逼近、盘中异常、重要证据、数据质量；
- AttentionEvent 只陈述事实（OPEN），绝不改变 Decision、绝不产生 Trade；
- §10.4 去抖：同一 dedupe_key 在 cooldown_seconds 内绝不重复创建；
  不同事件类型 dedupe_key 不同，STOP_NEAR → STOP_HIT 升级不被冷却挡住；
- 没有客观事实就不产生事件（计划未写 stop/target → 零事件）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID

from app.v3.domain.attention import (
    AttentionEventType,
    IntradayAttentionEvent,
)
from app.v3.domain.hashing import canonical_hash

_NEAR_PCT = 0.01  # 距离 stop/target 1% 以内视为"逼近"
_ENGINE_VERSION = "attention-engine-v1"


def _price(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AttentionEvaluation:
    created: tuple[IntradayAttentionEvent, ...] = ()
    skipped: int = 0


class _AttentionRepo(Protocol):
    async def last_known_at(self, dedupe_key: str) -> datetime | None: ...
    async def save(self, event: IntradayAttentionEvent) -> IntradayAttentionEvent: ...


class _Uow(Protocol):
    attention: _AttentionRepo

    async def commit(self) -> None: ...

    async def __aenter__(self) -> "_Uow": ...

    async def __aexit__(self, *args) -> None: ...


class AttentionEngineService:
    def __init__(
        self,
        uow_factory,
        *,
        cooldown_seconds: float = 600.0,
        materiality_threshold: float = 0.7,
        clock=None,
    ) -> None:
        self._uow_factory = uow_factory
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._materiality_threshold = materiality_threshold
        self._clock = clock or (lambda: datetime.now(datetime.now().astimezone().tzinfo))

    async def _emit(
        self, drafts: list[IntradayAttentionEvent],
    ) -> AttentionEvaluation:
        created: list[IntradayAttentionEvent] = []
        skipped = 0
        async with self._uow_factory() as uow:
            for draft in drafts:
                last = await uow.attention.last_known_at(draft.dedupe_key)
                if last is not None and draft.known_at - last < self._cooldown:
                    skipped += 1
                    continue
                await uow.attention.save(draft)
                created.append(draft)
            await uow.commit()
        return AttentionEvaluation(created=tuple(created), skipped=skipped)

    @staticmethod
    def _draft(
        *, event_type: AttentionEventType, dedupe_key: str, severity: str,
        facts: dict[str, Any], as_of: datetime, known_at: datetime,
        code: str | None = None, market: str | None = None,
        security_id: UUID | None = None, entry_plan_id: UUID | None = None,
        account_id: UUID | None = None, subject_type: str = "SECURITY",
        source_snapshot_ids: list[str] | None = None,
    ) -> IntradayAttentionEvent:
        return IntradayAttentionEvent(
            subject_type=subject_type, security_id=security_id,
            code=code, market=market, account_id=account_id,
            entry_plan_id=entry_plan_id, event_type=event_type,
            severity=severity, facts=facts, as_of=as_of, known_at=known_at,
            source_snapshot_ids=source_snapshot_ids or [],
            dedupe_key=dedupe_key,
            content_hash=canonical_hash({
                "event_type": str(event_type), "dedupe_key": dedupe_key,
                "facts": facts, "as_of": as_of.isoformat(),
                "engine": _ENGINE_VERSION,
            }),
        )

    async def evaluate_entry_plan_levels(
        self,
        *,
        entry_plan_id: UUID,
        security_id: UUID,
        code: str,
        market: str,
        plan: dict[str, Any],
        quote: Any,
        as_of: datetime,
    ) -> AttentionEvaluation:
        """持仓/计划级价格触发（§9.3 Price Trigger 的确定性部分）。"""
        known_at = self._clock()
        last_price = _price(getattr(quote, "last_price", None))
        stop = _price(plan.get("stop_loss"))
        target = _price(plan.get("take_profit"))
        drafts: list[IntradayAttentionEvent] = []
        # R4-P1-002：停牌 / stale / 不可信（R4-P0-001 未来事实降级）Quote
        # 绝不产生确定性 STOP_HIT/TARGET_HIT/NEAR——价格触发整体跳过，
        # 只发 DATA_QUALITY_DEGRADED（on-demand 与常驻 Loop 同一防线）。
        suspended = bool(getattr(quote, "suspended", False))
        stale = bool(getattr(quote, "stale", False))
        quality = getattr(quote, "quality", None) or ""
        untrusted = quality == "UNTRUSTED"
        if suspended or stale or untrusted:
            reason = (
                "SUSPENDED" if suspended
                else "UNTRUSTED_QUALITY" if untrusted
                else "STALE_QUOTE"
            )
            drafts.append(self._draft(
                event_type=AttentionEventType.DATA_QUALITY_DEGRADED,
                dedupe_key=f"DATA_QUALITY_DEGRADED:{market}:{code}",
                severity="WARNING",
                facts={"reason": reason, "quality": quality,
                       "suspended": suspended, "stale": stale,
                       "last_price": last_price},
                as_of=as_of, known_at=known_at, code=code, market=market,
            ))
            return await self._emit(drafts)
        if last_price is not None and stop is not None:
            if last_price <= stop:
                drafts.append(self._draft(
                    event_type=AttentionEventType.STOP_HIT,
                    dedupe_key=f"STOP_HIT:{entry_plan_id}",
                    severity="CRITICAL",
                    facts={"last_price": last_price, "stop_loss": stop,
                           "plan": "stop_loss breached"},
                    as_of=as_of, known_at=known_at, code=code, market=market,
                    security_id=security_id, entry_plan_id=entry_plan_id,
                    subject_type="ENTRY_PLAN",
                ))
            elif (last_price - stop) / last_price <= _NEAR_PCT:
                drafts.append(self._draft(
                    event_type=AttentionEventType.STOP_NEAR,
                    dedupe_key=f"STOP_NEAR:{entry_plan_id}",
                    severity="WARNING",
                    facts={"last_price": last_price, "stop_loss": stop},
                    as_of=as_of, known_at=known_at, code=code, market=market,
                    security_id=security_id, entry_plan_id=entry_plan_id,
                    subject_type="ENTRY_PLAN",
                ))
        if last_price is not None and target is not None:
            if last_price >= target:
                drafts.append(self._draft(
                    event_type=AttentionEventType.TARGET_HIT,
                    dedupe_key=f"TARGET_HIT:{entry_plan_id}",
                    severity="WARNING",
                    facts={"last_price": last_price, "take_profit": target},
                    as_of=as_of, known_at=known_at, code=code, market=market,
                    security_id=security_id, entry_plan_id=entry_plan_id,
                    subject_type="ENTRY_PLAN",
                ))
            elif (target - last_price) / target <= _NEAR_PCT:
                drafts.append(self._draft(
                    event_type=AttentionEventType.TARGET_NEAR,
                    dedupe_key=f"TARGET_NEAR:{entry_plan_id}",
                    severity="INFO",
                    facts={"last_price": last_price, "take_profit": target},
                    as_of=as_of, known_at=known_at, code=code, market=market,
                    security_id=security_id, entry_plan_id=entry_plan_id,
                    subject_type="ENTRY_PLAN",
                ))
        return await self._emit(drafts)

    async def record_intraday_anomalies(
        self, candidates: Iterable[Any], *, as_of: datetime,
    ) -> AttentionEvaluation:
        """§5.2 盘中异常 → INTRADAY_ANOMALY（按 reason 各自去抖）。"""
        known_at = self._clock()
        drafts: list[IntradayAttentionEvent] = []
        for candidate in candidates:
            for reason in candidate.reasons:
                severity = "CRITICAL" if reason == "SHARP_DROP" else "WARNING"
                drafts.append(self._draft(
                    event_type=AttentionEventType.INTRADAY_ANOMALY,
                    dedupe_key=f"INTRADAY_ANOMALY:{candidate.market}:{candidate.code}:{reason}",
                    severity=severity,
                    facts={
                        "reason": reason,
                        "reasons": list(candidate.reasons),
                        "last_price": candidate.latest_price,
                        "intraday_return": candidate.intraday_return,
                        "volume_ratio": candidate.volume_ratio,
                    },
                    as_of=as_of, known_at=known_at,
                    code=candidate.code, market=candidate.market,
                ))
        return await self._emit(drafts)

    async def record_new_evidence(
        self, items: Iterable[dict[str, Any]], *,
        universe: set[tuple[str, str]], as_of: datetime,
    ) -> AttentionEvaluation:
        """§6.3 重要证据 + 池内证券 → NEW_EVIDENCE（绝不自动改 Decision）。"""
        known_at = self._clock()
        drafts: list[IntradayAttentionEvent] = []
        for item in items:
            materiality = item.get("materiality")
            if materiality is None or materiality < self._materiality_threshold:
                continue
            key = (item.get("market"), item.get("code"))
            if key not in universe:
                continue
            evidence_id = str(item.get("evidence_id", ""))
            drafts.append(self._draft(
                event_type=AttentionEventType.NEW_EVIDENCE,
                dedupe_key=f"NEW_EVIDENCE:{evidence_id}",
                severity="INFO",
                facts={
                    "evidence_id": evidence_id,
                    "materiality": materiality,
                    "title": item.get("title"),
                },
                as_of=as_of, known_at=known_at,
                code=item.get("code"), market=item.get("market"),
                source_snapshot_ids=[evidence_id] if evidence_id else [],
            ))
        return await self._emit(drafts)

    async def record_data_quality(
        self, quotes: Iterable[Any], *,
        universe: set[tuple[str, str]], as_of: datetime,
    ) -> AttentionEvaluation:
        """§23 数据质量降级 → DATA_QUALITY_DEGRADED（仅池内证券）。"""
        known_at = self._clock()
        drafts: list[IntradayAttentionEvent] = []
        for quote in quotes:
            if not getattr(quote, "stale", False):
                continue
            key = (getattr(quote, "market", None), getattr(quote, "code", None))
            if key not in universe:
                continue
            drafts.append(self._draft(
                event_type=AttentionEventType.DATA_QUALITY_DEGRADED,
                dedupe_key=f"DATA_QUALITY_DEGRADED:{key[0]}:{key[1]}",
                severity="WARNING",
                facts={"quality": getattr(quote, "quality", None),
                       "last_price": getattr(quote, "last_price", None)},
                as_of=as_of, known_at=known_at,
                code=key[1], market=key[0],
            ))
        return await self._emit(drafts)
