from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.v3.domain.hashing import canonical_hash
from app.v3.domain.portfolio import AccountCreate, build_execution_deviation


NOW = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)

PLAN = {
    "entry_price_low": "10",
    "entry_price_high": "11",
    "quantity": "100",
    "entry_window_start": "2026-09-01T01:30:00+00:00",
    "entry_window_end": "2026-09-01T02:30:00+00:00",
    "trigger": {"type": "PRICE_ABOVE", "price": "10.5"},
    "cancel_condition": {"type": "PRICE_BELOW", "price": "9.5"},
}


def test_account_cost_method_is_limited_to_weighted_average() -> None:
    account = AccountCreate(name=f"cost-{uuid4().hex}")
    assert account.cost_method == "WEIGHTED_AVERAGE"
    account = AccountCreate(name=f"cost-{uuid4().hex}", cost_method="WEIGHTED_AVERAGE")
    assert account.cost_method == "WEIGHTED_AVERAGE"
    with pytest.raises(ValidationError, match="WEIGHTED_AVERAGE"):
        AccountCreate(name=f"cost-{uuid4().hex}", cost_method="FIFO")


def test_deviation_inside_window_with_satisfied_trigger() -> None:
    facts = {"price": "10.6"}
    deviation = build_execution_deviation(
        PLAN, Decimal("10.5"), Decimal("100"), NOW, trigger_facts=facts,
    )
    assert deviation["price_delta_to_entry_window"] == "0"
    assert deviation["price_delta_pct"] == "0"
    assert deviation["price_window_relation"] == "INSIDE"
    assert deviation["entry_window_start"] == "2026-09-01T01:30:00+00:00"
    assert deviation["entry_window_end"] == "2026-09-01T02:30:00+00:00"
    assert deviation["time_window_relation"] == "INSIDE"
    assert deviation["session_delta_minutes"] == "0"
    assert deviation["quantity_delta"] == "0"
    assert deviation["quantity_delta_pct"] == "0"
    assert deviation["trigger_match"] == "MATCH"
    assert deviation["cancel_condition_violated"] == "NOT_VIOLATED"
    assert deviation["trade_time"] == NOW.isoformat()
    assert deviation["trigger_facts_hash"] == canonical_hash(facts)
    assert deviation["plan_snapshot_hash"] == canonical_hash(PLAN)


def test_deviation_above_window_reports_price_and_late_time() -> None:
    deviation = build_execution_deviation(
        PLAN, Decimal("12"), Decimal("80"),
        datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
        trigger_facts={"price": "12"},
    )
    assert deviation["price_delta_to_entry_window"] == "1"
    assert deviation["price_delta_pct"] == "9.0909"
    assert deviation["price_window_relation"] == "ABOVE"
    assert deviation["time_window_relation"] == "AFTER"
    assert deviation["session_delta_minutes"] == "30"
    assert deviation["quantity_delta"] == "-20"
    assert deviation["quantity_delta_pct"] == "-20.0000"
    assert deviation["trigger_match"] == "MATCH"


def test_deviation_below_window_reports_early_time_and_violated_cancel() -> None:
    deviation = build_execution_deviation(
        PLAN, Decimal("9"), Decimal("100"),
        datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc),
        trigger_facts={"price": "9.0"},
    )
    assert deviation["price_delta_to_entry_window"] == "-1"
    assert deviation["price_delta_pct"] == "-10.0000"
    assert deviation["price_window_relation"] == "BELOW"
    assert deviation["time_window_relation"] == "BEFORE"
    assert deviation["session_delta_minutes"] == "-30"
    assert deviation["trigger_match"] == "NOT_MATCH"
    assert deviation["cancel_condition_violated"] == "VIOLATED"


def test_deviation_without_price_window_stays_unknown() -> None:
    plan = {"quantity": "100", "trigger": PLAN["trigger"]}
    deviation = build_execution_deviation(
        plan, Decimal("10.5"), Decimal("100"), NOW, trigger_facts={"price": "10.6"},
    )
    assert deviation["price_delta_to_entry_window"] == "0"
    assert deviation["price_delta_pct"] == "UNKNOWN"
    assert deviation["price_window_relation"] == "UNKNOWN"
    assert deviation["time_window_relation"] == "UNKNOWN"
    assert deviation["session_delta_minutes"] == "UNKNOWN"
    assert deviation["entry_window_start"] is None
    assert deviation["entry_window_end"] is None
    assert deviation["trigger_match"] == "MATCH"


def test_deviation_without_time_window_stays_unknown() -> None:
    plan = {key: value for key, value in PLAN.items() if not key.startswith("entry_window")}
    deviation = build_execution_deviation(
        plan, Decimal("10.5"), Decimal("100"), NOW,
    )
    assert deviation["time_window_relation"] == "UNKNOWN"
    assert deviation["session_delta_minutes"] == "UNKNOWN"
    assert deviation["entry_window_start"] is None
    assert deviation["entry_window_end"] is None


def test_deviation_quantity_without_plan_quantity_stays_unknown() -> None:
    plan = {key: value for key, value in PLAN.items() if key != "quantity"}
    deviation = build_execution_deviation(
        plan, Decimal("10.5"), Decimal("100"), NOW,
    )
    assert deviation["quantity_delta"] == "UNKNOWN"
    assert deviation["quantity_delta_pct"] == "UNKNOWN"


def test_deviation_trigger_without_facts_stays_unknown() -> None:
    deviation = build_execution_deviation(PLAN, Decimal("10.5"), Decimal("100"), NOW)
    assert deviation["trigger_match"] == "UNKNOWN"
    assert deviation["cancel_condition_violated"] == "UNKNOWN"
    assert deviation["trigger_facts_hash"] is None


def test_deviation_trigger_without_plan_trigger_stays_unknown() -> None:
    plan = {key: value for key, value in PLAN.items() if key != "trigger"}
    deviation = build_execution_deviation(
        plan, Decimal("10.5"), Decimal("100"), NOW, trigger_facts={"price": "10.6"},
    )
    assert deviation["trigger_match"] == "UNKNOWN"


def test_deviation_unsupported_trigger_type_stays_unknown() -> None:
    plan = {**PLAN, "trigger": {"type": "VOLUME_SPIKE"}}
    deviation = build_execution_deviation(
        plan, Decimal("10.5"), Decimal("100"), NOW, trigger_facts={"price": "10.6"},
    )
    assert deviation["trigger_match"] == "UNKNOWN"


def test_deviation_facts_without_price_stay_unknown() -> None:
    deviation = build_execution_deviation(
        PLAN, Decimal("10.5"), Decimal("100"), NOW, trigger_facts={"volume": "1"},
    )
    assert deviation["trigger_match"] == "UNKNOWN"
    assert deviation["cancel_condition_violated"] == "UNKNOWN"


def test_deviation_in_range_trigger_matches_only_inside_range() -> None:
    plan = {
        **PLAN,
        "trigger": {"type": "PRICE_IN_RANGE", "low": "10", "high": "11"},
    }
    matched = build_execution_deviation(
        plan, Decimal("10.5"), Decimal("100"), NOW, trigger_facts={"price": "10.6"},
    )
    assert matched["trigger_match"] == "MATCH"
    missed = build_execution_deviation(
        plan, Decimal("12"), Decimal("100"), NOW, trigger_facts={"price": "12"},
    )
    assert missed["trigger_match"] == "NOT_MATCH"
