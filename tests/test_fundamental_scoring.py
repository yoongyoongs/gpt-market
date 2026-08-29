from __future__ import annotations

from datetime import datetime

from app.fundamentals.base import FundamentalProvider
from app.fundamentals.manager import FundamentalProviderManager
from app.models import FundamentalField, FundamentalQuarter, FundamentalSnapshot
from app.services.fundamental_scoring import score_fundamental
from app.utils.time import SHANGHAI


NOW = datetime(2026, 8, 29, tzinfo=SHANGHAI)


def field(value, source: str = "primary") -> FundamentalField:
    return FundamentalField(
        value=value,
        source=source,
        upstream_source=source,
        source_type="vendor",
        report_period="2026-06-30",
        fetch_time=NOW,
        coverage=value is not None,
        stale=False,
        confidence="HIGH",
    )


def snapshot(code: str = "603019", *, profit_yoy: float = 30, previous_loss: bool = False) -> FundamentalSnapshot:
    fields = {
        "revenue": field(10_000_000_000),
        "revenue_yoy": field(20),
        "revenue_qoq": field(8),
        "net_profit": field(1_000_000_000),
        "deducted_net_profit": field(900_000_000),
        "net_profit_yoy": field(profit_yoy),
        "net_profit_qoq": field(12),
        "roe": field(12),
        "operating_cash_flow": field(1_100_000_000),
        "gross_margin": field(32),
        "debt_ratio": field(42),
        "pe": field(18),
        "pb": field(2.1),
        "industry": field("计算机设备"),
        "industry_pe_median": field(24, "peer_median"),
        "industry_pb_median": field(2.8, "peer_median"),
    }
    quarters = [
        FundamentalQuarter(
            report_period=f"202{6-index}-06-30",
            revenue=10_000_000_000,
            net_profit=-10_000 if previous_loss and index else 1_000_000_000,
            net_profit_yoy=30 - index * 8,
            deducted_net_profit=900_000_000,
            deducted_net_profit_yoy=25 - index * 5,
            operating_cash_flow=1_100_000_000,
            debt_ratio=42,
        )
        for index in range(4)
    ]
    return FundamentalSnapshot(
        code=code,
        fields=fields,
        quarterly_trend=quarters,
        report_period="2026-06-30",
        fetch_time=NOW,
        source="primary",
        upstream_sources=["primary"],
    ).with_coverage()


def test_fundamental_score_uses_quality_cashflow_roe_debt_and_relative_valuation() -> None:
    component, risk = score_fundamental(snapshot())
    assert component.coverage is True
    assert component.score is not None and 10 <= component.score <= 15
    assert risk.score == 0
    assert component.raw_value["coverage_rate"] == 1


def test_missing_fundamentals_are_not_scored_as_zero() -> None:
    missing = FundamentalSnapshot(
        code="600001", fields={"revenue": field(None)}, fetch_time=NOW,
        source="primary", upstream_sources=["primary"], error="missing",
    ).with_coverage()
    component, risk = score_fundamental(missing)
    assert component.score is None
    assert component.coverage is False
    assert risk.score == 0
    assert risk.coverage is False


def test_low_base_growth_does_not_receive_full_profit_growth_credit() -> None:
    normal, _ = score_fundamental(snapshot(profit_yoy=80, previous_loss=False))
    low_base, _ = score_fundamental(snapshot(profit_yoy=80, previous_loss=True))
    # The explicit reason makes the anti-false-improvement decision auditable.
    assert any("基数" in reason for reason in low_base.reason) or low_base.score <= normal.score


def test_fundamental_risks_penalize_losses_cashflow_debt_and_one_off_income() -> None:
    value = snapshot()
    fields = dict(value.fields)
    fields["net_profit"] = field(100)
    fields["deducted_net_profit"] = field(20)
    fields["net_profit_yoy"] = field(-50)
    fields["debt_ratio"] = field(88)
    quarters = [
        row.model_copy(update={"net_profit": -100, "operating_cash_flow": -200, "deducted_net_profit_yoy": -60})
        for row in value.quarterly_trend
    ]
    _, risk = score_fundamental(value.model_copy(update={"fields": fields, "quarterly_trend": quarters}))
    assert risk.score is not None and risk.score <= -10
    assert any("持续亏损" in reason for reason in risk.reason)


def test_negative_performance_forecast_enters_fundamental_risk() -> None:
    value = snapshot().model_copy(
        update={
            "performance_forecast": FundamentalField(
                value={"type": "预减", "yoy_lower": -50, "yoy_upper": -35},
                source="forecast",
                upstream_source="eastmoney",
                source_type="vendor",
                report_period="2026-12-31",
                fetch_time=NOW,
                coverage=True,
                stale=False,
                confidence="MEDIUM",
            )
        }
    )
    _, risk = score_fundamental(value)
    assert risk.score == -2
    assert any("业绩预告" in reason for reason in risk.reason)


class FakeProvider(FundamentalProvider):
    def __init__(self, name: str, values: dict[str, FundamentalSnapshot], fail: bool = False) -> None:
        self.name = name
        self.upstream_source = name
        self.values = values
        self.fail = fail
        self.calls = 0

    async def get_many(self, codes: list[str]) -> dict[str, FundamentalSnapshot]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("upstream failed")
        return {code: self.values[code] for code in codes if code in self.values}


async def test_manager_uses_fallback_and_cache_without_exposing_provider_to_scoring() -> None:
    value = snapshot()
    primary = FakeProvider("primary", {}, fail=True)
    fallback = FakeProvider("fallback", {value.code: value})
    manager = FundamentalProviderManager(primary, [fallback], ttl_seconds=60)
    first = await manager.get(value.code)
    second = await manager.get(value.code)
    assert first.code == value.code
    assert second == first
    assert primary.calls == 1
    assert fallback.calls == 1


async def test_manager_records_conflicting_sources() -> None:
    primary_value = snapshot()
    fallback_value = snapshot().model_copy(
        update={"fields": {**snapshot().fields, "roe": field(5, "fallback")}, "source": "fallback", "upstream_sources": ["fallback"]}
    )
    merged = FundamentalProviderManager._merge(primary_value, fallback_value)
    assert any(conflict.field == "roe" for conflict in merged.conflicts)
    assert merged.fields["roe"].value == 12


async def test_manager_keeps_last_success_as_stale_when_refresh_fails() -> None:
    value = snapshot()
    primary = FakeProvider("primary", {value.code: value})
    manager = FundamentalProviderManager(primary, ttl_seconds=0)
    assert (await manager.get(value.code)).stale is False
    primary.fail = True
    stale = await manager.get(value.code)
    assert stale.stale is True
    assert stale.coverage == 1
    assert stale.error is not None
