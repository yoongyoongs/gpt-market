"""RT-04：AttentionEvent 与 Trigger Engine（实时方案 §6.3/§10/§27 RT-04）。

- 客观条件评估是确定性的：stop/target 命中与逼近、盘中异常、重要证据、
  数据质量劣化 → AttentionEvent（OPEN），绝不改变 Decision、绝不产生 Trade；
- §10.4 去抖：同一 dedupe_key 在冷却窗口内绝不重复创建
  （STOP_HIT 已触发不会每 10 秒刷 100 条）；
- 不同事件类型互不阻塞（STOP_NEAR 升级 STOP_HIT 不被冷却挡住）；
- 没有客观事实就不产生事件（无 stop/take_profit 的计划 → 零事件）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.v3.application.attention_engine import AttentionEngineService
from app.v3.domain.attention import (
    AttentionEventType,
    AttentionStatus,
    IntradayAttentionEvent,
)

NOW = datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc)


class _FakeAttentionRepo:
    def __init__(self):
        self.saved: list[IntradayAttentionEvent] = []

    async def last_known_at(self, dedupe_key: str):
        times = [
            event.known_at for event in self.saved
            if event.dedupe_key == dedupe_key
        ]
        return max(times) if times else None

    async def save(self, event: IntradayAttentionEvent) -> IntradayAttentionEvent:
        self.saved.append(event)
        return event


class _FakeUow:
    def __init__(self, repo: _FakeAttentionRepo):
        self.attention = repo
        self.committed = False

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _engine(repo, *, cooldown_seconds: float = 600.0):
    return AttentionEngineService(
        lambda: _FakeUow(repo), cooldown_seconds=cooldown_seconds,
        clock=lambda: NOW,
    )


def _quote(price: float, code: str = "000001"):
    from datetime import timedelta

    from app.v3.domain.intraday import IntradayQuoteSnapshot

    return IntradayQuoteSnapshot(
        code=code, market="SZ", name="测试", last_price=price,
        open=price, high=price, low=price, prev_close=price,
        change=0.0, change_pct=0.0, volume=1, amount=1.0,
        turnover_rate=1.0, volume_ratio=1.0,
        event_time=NOW - timedelta(seconds=3), fetch_time=NOW,
        known_at=NOW, as_of=NOW, source="test", upstream_source="test",
        quality="LIVE", stale=False, confidence="HIGH",
    )


_PLAN = {
    "stop_loss": "9.0", "take_profit": "12.0",
    "invalidation": "跌破 9.0 失效",
}


@pytest.mark.asyncio
async def test_stop_hit_creates_critical_event_once_within_cooldown() -> None:
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    plan_id = uuid4()

    first = await engine.evaluate_entry_plan_levels(
        entry_plan_id=plan_id, security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=_quote(8.9), as_of=NOW,
    )
    assert first.created[0].event_type == AttentionEventType.STOP_HIT
    assert first.created[0].severity == "CRITICAL"
    assert first.created[0].status == AttentionStatus.OPEN
    assert first.created[0].dedupe_key == f"STOP_HIT:{plan_id}"
    assert first.created[0].facts["last_price"] == 8.9

    # 冷却窗口内同一条件：绝不重复创建
    second = await engine.evaluate_entry_plan_levels(
        entry_plan_id=plan_id, security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=_quote(8.85), as_of=NOW + timedelta(seconds=30),
    )
    assert second.created == ()
    assert second.skipped == 1
    assert len(repo.saved) == 1


@pytest.mark.asyncio
async def test_stop_near_upgrades_to_stop_hit_across_types() -> None:
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    plan_id = uuid4()

    near = await engine.evaluate_entry_plan_levels(
        entry_plan_id=plan_id, security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=_quote(9.05), as_of=NOW,
    )
    assert near.created[0].event_type == AttentionEventType.STOP_NEAR
    assert near.created[0].severity == "WARNING"

    hit = await engine.evaluate_entry_plan_levels(
        entry_plan_id=plan_id, security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=_quote(8.99), as_of=NOW + timedelta(seconds=30),
    )
    # 不同事件类型互不阻塞：STOP_NEAR 不挡 STOP_HIT
    assert hit.created[0].event_type == AttentionEventType.STOP_HIT


@pytest.mark.asyncio
async def test_target_near_and_hit_evaluated() -> None:
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    plan_id = uuid4()
    near = await engine.evaluate_entry_plan_levels(
        entry_plan_id=plan_id, security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=_quote(11.95), as_of=NOW,
    )
    assert near.created[0].event_type == AttentionEventType.TARGET_NEAR
    hit = await engine.evaluate_entry_plan_levels(
        entry_plan_id=plan_id, security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=_quote(12.1), as_of=NOW,
    )
    assert hit.created[0].event_type == AttentionEventType.TARGET_HIT


@pytest.mark.asyncio
async def test_plan_without_levels_creates_nothing() -> None:
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    report = await engine.evaluate_entry_plan_levels(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan={"invalidation": "只写失效条件"}, quote=_quote(9.5),
        as_of=NOW,
    )
    assert report.created == ()
    assert report.skipped == 0


@pytest.mark.asyncio
async def test_intraday_anomalies_become_events_per_reason() -> None:
    from app.v3.domain.intraday import IntradayAttentionCandidate

    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    candidates = (
        IntradayAttentionCandidate(
            code="000001", market="SZ", as_of=NOW, known_at=NOW,
            source="scanner", reasons=("VOLUME_SURGE", "BREAKOUT_20D"),
            latest_price=9.9, intraday_return=0.07, volume_ratio=2.5,
        ),
    )
    report = await engine.record_intraday_anomalies(candidates, as_of=NOW)
    types = {event.event_type for event in report.created}
    assert types == {AttentionEventType.INTRADAY_ANOMALY}
    keys = {event.dedupe_key for event in report.created}
    assert keys == {
        "INTRADAY_ANOMALY:SZ:000001:VOLUME_SURGE",
        "INTRADAY_ANOMALY:SZ:000001:BREAKOUT_20D",
    }


@pytest.mark.asyncio
async def test_new_evidence_gated_by_materiality_and_universe() -> None:
    repo = _FakeAttentionRepo()
    engine = _engine(repo, cooldown_seconds=0.0)
    universe = {("SZ", "000001")}
    items = [
        {"code": "000001", "market": "SZ", "evidence_id": str(uuid4()),
         "materiality": 0.9, "title": "重大合同"},
        {"code": "000001", "market": "SZ", "evidence_id": str(uuid4()),
         "materiality": 0.3, "title": "例行公告"},       # 低于阈值 → 忽略
        {"code": "600000", "market": "SH", "evidence_id": str(uuid4()),
         "materiality": 0.9, "title": "池外证券"},        # 不在池内 → 忽略
    ]
    report = await engine.record_new_evidence(items, universe=universe, as_of=NOW)
    assert len(report.created) == 1
    assert report.created[0].event_type == AttentionEventType.NEW_EVIDENCE


@pytest.mark.asyncio
async def test_data_quality_degraded_for_stale_universe_quote() -> None:
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    stale_quote = _quote(9.5)
    stale_quote = stale_quote.model_copy(update={"stale": True, "quality": "STALE"})
    report = await engine.record_data_quality(
        [stale_quote], universe={("SZ", "000001")}, as_of=NOW,
    )
    assert len(report.created) == 1
    assert report.created[0].event_type == AttentionEventType.DATA_QUALITY_DEGRADED
    # 非 stale 或池外 → 零事件
    fresh = await engine.record_data_quality(
        [_quote(9.5)], universe={("SZ", "000001")}, as_of=NOW,
    )
    assert fresh.created == ()

# ---------- R4-P1-002：stale/suspended Quote 不得触发价格 Attention ----------


@pytest.mark.asyncio
async def test_stale_quote_never_creates_stop_hit() -> None:
    """R4-P1-002 §27：stale quote + 价格破 stop → 绝不 STOP_HIT，
    只允许 DATA_QUALITY_DEGRADED。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    stale = _quote(8.5).model_copy(update={"stale": True, "quality": "STALE"})
    report = await engine.evaluate_entry_plan_levels(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=stale, as_of=NOW,
    )
    types = {event.event_type for event in report.created}
    assert AttentionEventType.STOP_HIT not in types
    assert AttentionEventType.STOP_NEAR not in types
    assert types == {AttentionEventType.DATA_QUALITY_DEGRADED}
    assert report.created[0].facts["reason"] == "STALE_QUOTE"


@pytest.mark.asyncio
async def test_suspended_quote_never_creates_target_hit() -> None:
    """R4-P1-002 §27：停牌 + 价格达标 → 绝不 TARGET_HIT。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    suspended = _quote(12.5).model_copy(update={"suspended": True})
    report = await engine.evaluate_entry_plan_levels(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=suspended, as_of=NOW,
    )
    types = {event.event_type for event in report.created}
    assert AttentionEventType.TARGET_HIT not in types
    assert AttentionEventType.TARGET_NEAR not in types
    assert types == {AttentionEventType.DATA_QUALITY_DEGRADED}
    assert report.created[0].facts["reason"] == "SUSPENDED"


@pytest.mark.asyncio
async def test_untrusted_future_quote_never_creates_hits() -> None:
    """R4-P0-001 联动：未来事实降级 UNTRUSTED 的 Quote 不产生确定性触发。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    untrusted = _quote(8.5).model_copy(update={
        "stale": True, "quality": "UNTRUSTED",
    })
    report = await engine.evaluate_entry_plan_levels(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=untrusted, as_of=NOW,
    )
    types = {event.event_type for event in report.created}
    assert types == {AttentionEventType.DATA_QUALITY_DEGRADED}
    assert report.created[0].facts["reason"] == "UNTRUSTED_QUALITY"


@pytest.mark.asyncio
async def test_fresh_quote_still_triggers_hits() -> None:
    """Gate 只挡坏数据：新鲜 Quote 的 STOP_HIT 照常工作。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    plan_id = uuid4()
    report = await engine.evaluate_entry_plan_levels(
        entry_plan_id=plan_id, security_id=uuid4(), code="000001",
        market="SZ", plan=_PLAN, quote=_quote(8.9), as_of=NOW,
    )
    assert report.created[0].event_type == AttentionEventType.STOP_HIT


# ---------- R5-P1-010/§33/§66：后台常驻 typed Entry Trigger/Cancel ----------

_TYPED_PLAN = {
    "entry_mode": "PULLBACK_ENTRY",
    "entry_zone": {"low": 9.28, "high": 9.36},
    "triggers": [
        {"kind": "PRICE_ABOVE", "value": 9.30},
        {"kind": "TEXT", "description": "60m 支撑企稳"},
    ],
    "cancels": [{"kind": "PRICE_BELOW", "value": 9.02}],
    "stop": {"price": 9.02, "reason": "跌破前低失效"},
    "targets": [{"price": 9.85, "target_type": "T1"}],
}


def _types(report):
    return {event.event_type for event in report.created}


@pytest.mark.asyncio
async def test_entry_trigger_met_when_all_objective_triggers_satisfied() -> None:
    """WAIT_ENTRY + 价格满足全部客观 Trigger → ENTRY_TRIGGER_MET
    （TEXT 条件不阻塞客观判定，也不假装已满足）。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    report = await engine.evaluate_entry_trigger_cancel(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan=_TYPED_PLAN, quote=_quote(9.41), as_of=NOW,
    )
    assert report.created[0].event_type == AttentionEventType.ENTRY_TRIGGER_MET
    assert report.created[0].facts["met"] == ["PRICE_ABOVE@9.3"]
    assert report.created[0].facts["entry_mode"] == "PULLBACK_ENTRY"


@pytest.mark.asyncio
async def test_entry_trigger_near_when_price_close_to_trigger() -> None:
    """距客观 Trigger 1% 以内但未满足 → ENTRY_TRIGGER_NEAR。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    report = await engine.evaluate_entry_trigger_cancel(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan=_TYPED_PLAN, quote=_quote(9.27), as_of=NOW,
    )
    assert report.created[0].event_type == AttentionEventType.ENTRY_TRIGGER_NEAR
    assert report.created[0].facts["near"] == ["PRICE_ABOVE@9.3"]


@pytest.mark.asyncio
async def test_entry_cancel_met_emits_warning() -> None:
    """客观 Cancel 满足 → ENTRY_CANCEL_MET（不产生 MET）。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    report = await engine.evaluate_entry_trigger_cancel(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan=_TYPED_PLAN, quote=_quote(9.0), as_of=NOW,
    )
    types = _types(report)
    assert AttentionEventType.ENTRY_CANCEL_MET in types
    assert AttentionEventType.ENTRY_TRIGGER_MET not in types
    cancel = next(
        event for event in report.created
        if event.event_type == AttentionEventType.ENTRY_CANCEL_MET
    )
    assert cancel.facts["met"] == ["PRICE_BELOW@9.02"]


@pytest.mark.asyncio
async def test_stale_quote_blocks_entry_trigger() -> None:
    """§66 stale 场景：stale Quote → NO ENTRY_TRIGGER_MET，只允许
    DATA_QUALITY_DEGRADED（与 stop/target 同一 Gate）。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    stale = _quote(9.41).model_copy(update={"stale": True, "quality": "STALE"})
    report = await engine.evaluate_entry_trigger_cancel(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan=_TYPED_PLAN, quote=stale, as_of=NOW,
    )
    types = _types(report)
    assert types == {AttentionEventType.DATA_QUALITY_DEGRADED}
    assert report.created[0].facts["reason"] == "STALE_QUOTE"


@pytest.mark.asyncio
async def test_suspended_quote_blocks_entry_trigger() -> None:
    """§66 suspended 场景：停牌 → 无 ENTRY_TRIGGER_MET。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    suspended = _quote(9.41).model_copy(update={"suspended": True})
    report = await engine.evaluate_entry_trigger_cancel(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan=_TYPED_PLAN, quote=suspended, as_of=NOW,
    )
    assert _types(report) == {AttentionEventType.DATA_QUALITY_DEGRADED}


@pytest.mark.asyncio
async def test_text_only_plan_creates_zero_events() -> None:
    """只有 TEXT Trigger 的计划没有客观事实 → 零事件（绝不假装判定）。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    report = await engine.evaluate_entry_trigger_cancel(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ",
        plan={"entry_mode": "X", "triggers": [
            {"kind": "TEXT", "description": "60m 支撑企稳"},
        ]},
        quote=_quote(9.41), as_of=NOW,
    )
    assert report.created == ()
    assert report.skipped == 0


@pytest.mark.asyncio
async def test_unparseable_plan_creates_zero_events() -> None:
    """plan 不可解析 → 零事件（不抛错、不阻断循环）。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    report = await engine.evaluate_entry_trigger_cancel(
        entry_plan_id=uuid4(), security_id=uuid4(), code="000001",
        market="SZ", plan={"triggers": "not-a-list"}, quote=_quote(9.41),
        as_of=NOW,
    )
    assert report.created == ()


@pytest.mark.asyncio
async def test_structure_change_recorded_per_timeframe() -> None:
    """§66 Structure Change：趋势翻转 → STRUCTURE_CHANGED（按
    market/code/timeframe 去抖）。"""
    repo = _FakeAttentionRepo()
    engine = _engine(repo)
    report = await engine.record_structure_changes([
        {"market": "SZ", "code": "000001", "timeframe": "60m",
         "from_trend": "UP", "to_trend": "DOWN",
         "structure": {"support": 9.0, "resistance": 10.5}},
    ], as_of=NOW)
    event = report.created[0]
    assert event.event_type == AttentionEventType.STRUCTURE_CHANGED
    assert event.facts["timeframe"] == "60m"
    assert event.facts["from_trend"] == "UP"
    assert event.facts["to_trend"] == "DOWN"
    second = await engine.record_structure_changes([
        {"market": "SZ", "code": "000001", "timeframe": "60m",
         "from_trend": "UP", "to_trend": "DOWN", "structure": {}},
    ], as_of=NOW)
    assert second.created == ()  # cooldown 去抖
