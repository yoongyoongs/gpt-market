"""R5-P1-005/§60：Active Plan 冻结定义纯函数验收。

Resident Monitor 只监控当前有效 EntryPlan：历史 Decision、
CLOSED/INVALIDATED Watchlist、未来生效计划、无 stop/target 的
Plan 一律排除（§51 验收矩阵）。
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


def _plan(decision_id, *, stop=9.0, target=11.0, effective_from=None, version=1):
    return EntryPlanModel(
        entry_plan_id=uuid4(),
        decision_id=decision_id,
        version=version,
        effective_from=effective_from or NOW - timedelta(days=1),
        expected_horizon="D3_10",
        plan={"stop_loss": stop, "take_profit": target},
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


def test_position_source_with_zero_watchlist_presence():
    """持仓场景：quantity>0（POSITION）、Watchlist 不在 active 集合 →
    仍监控当前 Decision 的 Plan，plan_source=POSITION。"""
    security = _security("600000")
    decision_current = _decision(security.security_id, hours_ago=1)
    latest = {decision_current.decision_id: _plan(decision_current.decision_id)}
    rows = _filter_active_plans(
        latest, [decision_current], {security.security_id: security},
        set(), {security.security_id}, NOW,
    )
    assert len(rows) == 1
    assert rows[0]["plan_source"] == "POSITION"


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
