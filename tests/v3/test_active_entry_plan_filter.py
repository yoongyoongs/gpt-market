"""R5-P1-005/§60 + F6-03：Active Plan 冻结定义纯函数验收。

Resident Monitor 只监控当前有效 EntryPlan：
- 历史 Decision、CLOSED/INVALIDATED Watchlist、未来生效计划排除；
- 最新 Decision 无 Plan → NO_ACTIVE_ENTRY_PLAN，绝不回退旧 Plan（Case D）；
- POSITION 优先 Trade-bound Plan，无绑定 → NO_TRADE_PLAN_BINDING
  不回退（Case C / P0-P1-04）；
- trigger-only 合法进入监控（Case E / P1-06）；
- max_wait_sessions 过期排除（P1-07）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.v3.infrastructure.db.decision_repositories import _filter_active_plans
from app.v3.infrastructure.db.models import (
    DecisionModel,
    EntryPlanModel,
    SecurityModel,
)

NOW = datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc)


def _plan(decision_id, *, stop=9.0, target=11.0, effective_from=None, version=1, plan=None):
    return EntryPlanModel(
        entry_plan_id=uuid4(),
        decision_id=decision_id,
        version=version,
        effective_from=effective_from or NOW - timedelta(days=1),
        expected_horizon="D3_10",
        plan=plan if plan is not None else {"stop_loss": stop, "take_profit": target},
    )


def _decision(security_id, *, hours_ago):
    return DecisionModel(
        decision_id=uuid4(),
        security_id=security_id,
        as_of=NOW - timedelta(hours=hours_ago),
    )


def _security(code):
    return SecurityModel(security_id=uuid4(), code=code, market="SZ")


def test_only_current_decision_plan_is_monitored():
    """§51：同 security Decision A（旧）/B（当前）——只监控 B 的 Plan。"""
    security = _security("000001")
    decision_old = _decision(security.security_id, hours_ago=72)
    decision_current = _decision(security.security_id, hours_ago=2)
    latest = {
        decision_old.decision_id: _plan(decision_old.decision_id, stop=8.0),
        decision_current.decision_id: _plan(decision_current.decision_id),
    }
    rows = _filter_active_plans(
        latest, [decision_old, decision_current],
        {security.security_id: security},
        {security.security_id}, set(), NOW,
    )
    assert len(rows) == 1
    assert rows[0]["decision_id"] == decision_current.decision_id
    assert rows[0]["stop_loss"] == 9.0  # 旧 Plan 的 8.0 不再监控
    assert rows[0]["plan_source"] == "ENTRY_WATCHLIST"


def test_invalidated_watchlist_never_monitored():
    """§51：Decision C 对应 Watchlist 已 INVALIDATED（不在 active 集合）
    且无持仓 → 不产生任何当前 Attention。"""
    security = _security("000003")
    decision_c = _decision(security.security_id, hours_ago=5)
    latest = {decision_c.decision_id: _plan(decision_c.decision_id)}
    rows = _filter_active_plans(
        latest, [decision_c], {security.security_id: security},
        set(), set(), NOW,
    )
    assert rows == []


def test_position_without_trade_binding_is_no_trade_plan_binding():
    """F6-03/Case C 前置（P0-P1-04）：持仓 quantity>0 但无 Trade 绑定 →
    显式 NO_TRADE_PLAN_BINDING，绝不悄悄回退最新 Decision Plan。"""
    security = _security("600000")
    decision_current = _decision(security.security_id, hours_ago=1)
    latest = {decision_current.decision_id: _plan(decision_current.decision_id)}
    rows = _filter_active_plans(
        latest, [decision_current], {security.security_id: security},
        set(), {security.security_id}, NOW,
    )
    assert rows == []  # NO_TRADE_PLAN_BINDING：跳过，不替换


def test_trade_bound_plan_survives_newer_decision():
    """F6-03/Case C（P0-P1-04）：真实成交按 Plan A 买入，之后产生
    Decision B / Plan B → 后台仍监控 Trade-bound Plan A。"""
    security = _security("600000")
    decision_a = _decision(security.security_id, hours_ago=72)
    decision_b = _decision(security.security_id, hours_ago=1)
    plan_a = _plan(decision_a.decision_id, stop=8.0, target=12.0)
    plan_b = _plan(decision_b.decision_id, stop=9.5, target=10.5)
    latest = {
        decision_a.decision_id: plan_a,
        decision_b.decision_id: plan_b,
    }
    rows = _filter_active_plans(
        latest, [decision_a, decision_b],
        {security.security_id: security},
        set(), {security.security_id}, NOW,
        plan_by_id={plan_a.entry_plan_id: plan_a, plan_b.entry_plan_id: plan_b},
        trade_binding={security.security_id: (plan_a.entry_plan_id, 1)},
    )
    assert len(rows) == 1
    assert rows[0]["entry_plan_id"] == plan_a.entry_plan_id
    assert rows[0]["plan_binding"] == "TRADE_BOUND"
    assert rows[0]["stop_loss"] == 8.0  # Plan B 的 9.5 不替换 Plan A


def test_latest_decision_without_plan_is_no_active_entry_plan():
    """F6-03/Case D（P0-P1-05）：Old Decision + Plan，New Decision 无
    Plan → NO_ACTIVE_ENTRY_PLAN，绝不回退旧 Decision 的 Plan。"""
    security = _security("000001")
    decision_old = _decision(security.security_id, hours_ago=72)
    decision_new = _decision(security.security_id, hours_ago=1)
    plan_old = _plan(decision_old.decision_id, stop=8.0)
    latest = {decision_old.decision_id: plan_old}  # 新 Decision 无 Plan
    rows = _filter_active_plans(
        latest, [decision_old, decision_new],
        {security.security_id: security},
        {security.security_id}, set(), NOW,
    )
    assert rows == []


def test_trigger_only_plan_is_monitored():
    """F6-03/Case E（P1-06）：PRICE_ABOVE trigger-only（stop/target 均
    None）→ 合法进入 Resident Monitor。"""
    security = _security("002274")
    decision = _decision(security.security_id, hours_ago=1)
    plan = _plan(
        decision.decision_id, stop=None, target=None,
        plan={"triggers": [{"type": "PRICE_ABOVE", "price": 10.0}]},
    )
    latest = {decision.decision_id: plan}
    rows = _filter_active_plans(
        latest, [decision], {security.security_id: security},
        {security.security_id}, set(), NOW,
    )
    assert len(rows) == 1
    assert rows[0]["stop_loss"] is None
    assert rows[0]["plan"]["triggers"][0]["type"] == "PRICE_ABOVE"


def test_expired_max_wait_sessions_not_monitored():
    """F6-03/P1-07：max_wait_sessions=3，effective_from 距今超过 3 个
    交易日 → 计划过期，不再无限期作为 current plan。"""
    security = _security("000001")
    decision = _decision(security.security_id, hours_ago=24 * 30)
    plan = _plan(
        decision.decision_id,
        effective_from=NOW - timedelta(days=30),
        plan={
            "stop_loss": 9.0, "take_profit": 11.0, "max_wait_sessions": 3,
        },
    )
    latest = {decision.decision_id: plan}
    # 2026-09-04 周五 → 简化日历：工作日谓词
    rows = _filter_active_plans(
        latest, [decision], {security.security_id: security},
        {security.security_id}, set(), NOW,
        is_trading_day=lambda day: day.weekday() < 5,
    )
    assert rows == []


def test_max_wait_not_expired_still_monitored():
    security = _security("000001")
    decision = _decision(security.security_id, hours_ago=24)
    plan = _plan(
        decision.decision_id,
        effective_from=NOW - timedelta(days=1),
        plan={
            "stop_loss": 9.0, "take_profit": 11.0, "max_wait_sessions": 3,
        },
    )
    latest = {decision.decision_id: plan}
    rows = _filter_active_plans(
        latest, [decision], {security.security_id: security},
        {security.security_id}, set(), NOW,
        is_trading_day=lambda day: day.weekday() < 5,
    )
    assert len(rows) == 1


def test_future_effective_plan_not_monitored():
    security = _security("002274")
    decision = _decision(security.security_id, hours_ago=1)
    latest = {
        decision.decision_id: _plan(
            decision.decision_id, effective_from=NOW + timedelta(hours=6),
        ),
    }
    rows = _filter_active_plans(
        latest, [decision], {security.security_id: security},
        {security.security_id}, set(), NOW,
    )
    assert rows == []


def test_typed_plan_stop_and_target_extracted():
    """RT-06 类型化结构：stop.price / targets[].price 兼容提取。"""
    security = _security("600519")
    decision = _decision(security.security_id, hours_ago=1)
    plan = EntryPlanModel(
        entry_plan_id=uuid4(),
        decision_id=decision.decision_id,
        version=2,
        effective_from=NOW - timedelta(days=1),
        expected_horizon="D10_20",
        plan={"stop": {"price": 1600.0}, "targets": [{"price": 1900.0}]},
    )
    rows = _filter_active_plans(
        {decision.decision_id: plan}, [decision],
        {security.security_id: security},
        {security.security_id}, set(), NOW,
    )
    assert rows[0]["stop_loss"] == 1600.0
    assert rows[0]["take_profit"] == 1900.0


def test_plan_without_levels_not_monitored():
    security = _security("600300")
    decision = _decision(security.security_id, hours_ago=1)
    latest = {
        decision.decision_id: _plan(
            decision.decision_id, stop=None, target=None,
        ),
    }
    rows = _filter_active_plans(
        latest, [decision], {security.security_id: security},
        {security.security_id}, set(), NOW,
    )
    assert rows == []
